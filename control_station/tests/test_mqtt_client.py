from __future__ import annotations

import json
import queue
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from control_station.models import (
    Alarm,
    AlarmCode,
    AlarmSeverity,
    CommandResult,
    CommandSource,
    CommandType,
)
from control_station.mqtt_client import ThingsBoardMqttClient
from control_station.configuration import load_configuration
from control_station.runtime import StationRuntime


class FakeBus:
    def __init__(self) -> None:
        self.raw = 280
        self.coils = [False] * 8

    def connect(self):
        pass

    def close(self):
        pass

    def read_holding_registers(self, slave_id, address, count=1):
        return [self.raw]

    def write_coil(self, slave_id, address, enabled):
        self.coils[address] = enabled

    def read_coils(self, slave_id, address, count=1, *, retries=None):
        return self.coils[address : address + count]


class FakePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, topic, payload, qos):
        self.messages.append((topic, json.loads(payload), qos))
        return SimpleNamespace(rc=0)


class FakeController:
    def __init__(self) -> None:
        self.commands = []

    def process_command(self, command):
        self.commands.append(command)
        return CommandResult(command.command_id, True, "Accepted")


class BrokenCloudBridge:
    connected = False

    def process_pending_commands(self):
        raise OSError("broker unavailable")

    def observe_alarm(self):
        raise OSError("broker unavailable")

    def publish_telemetry(self):
        raise OSError("broker unavailable")


def _bridge():
    bridge = ThingsBoardMqttClient.__new__(ThingsBoardMqttClient)
    bridge._pending = queue.Queue(maxsize=100)
    bridge._client = FakePublisher()
    bridge._mqtt = SimpleNamespace(MQTT_ERR_SUCCESS=0)
    bridge.controller = FakeController()
    bridge.relay = None
    bridge.simulation = False
    bridge._connected = True
    bridge._connection_lock = threading.Lock()
    bridge._last_alarm_id = None
    bridge._last_alarm = None
    bridge._last_publish_warning = 0.0
    return bridge


class TestThingsBoardMqttClient(TestCase):
    def test_telemetry_publisher_waits_for_its_independent_one_second_tick(self):
        bridge = _bridge()
        bridge._telemetry_stop = threading.Event()
        bridge.publish_telemetry = Mock(side_effect=bridge._telemetry_stop.set)

        bridge._telemetry_loop()

        bridge.publish_telemetry.assert_called_once_with()

    def test_alarm_activation_and_clear_are_both_recorded(self):
        bridge = _bridge()
        alarm = Alarm(
            code=AlarmCode.OVER_TEMPERATURE,
            severity=AlarmSeverity.CRITICAL,
            message="Too hot",
        )

        activated = bridge._alarm_events(alarm)
        cleared = bridge._alarm_events(None)

        self.assertTrue(activated[0]["active"])
        self.assertEqual(activated[0]["code"], "OVER_TEMPERATURE")
        self.assertFalse(cleared[0]["active"])
        self.assertEqual(cleared[0]["alarm_id"], alarm.alarm_id)
        self.assertIsNotNone(cleared[0]["cleared_at_ms"])

    def test_cloud_failures_do_not_trip_local_control(self):
        runtime = StationRuntime(load_configuration(), bus=FakeBus())
        runtime.mqtt_client = BrokenCloudBridge()

        with self.assertLogs("control_station.runtime", level="ERROR") as logs:
            runtime.poll_once()

        state = runtime.controller.get_state_snapshot()
        self.assertTrue(state.sensor_connected)
        self.assertTrue(state.modbus_connected)
        self.assertEqual(state.mode.value, "OFF")
        self.assertFalse(state.mqtt_connected)
        self.assertTrue(any("local control continues" in item for item in logs.output))

    def test_rpc_is_queued_then_processed_by_controller(self):
        bridge = _bridge()
        message = SimpleNamespace(
            topic="v1/devices/me/rpc/request/42",
            payload=json.dumps(
                {
                    "method": "SET_TEMPERATURE",
                    "params": {
                        "value": 62.5,
                        "command_id": "cmd-1",
                        "timestamp_ms": 1_800_000_000_000,
                    },
                }
            ).encode(),
        )

        bridge._on_message(None, None, message)
        self.assertEqual(bridge.controller.commands, [])
        self.assertEqual(bridge._pending.qsize(), 1)
        bridge.process_pending_commands()

        command = bridge.controller.commands[0]
        self.assertIs(command.command_type, CommandType.SET_TEMPERATURE)
        self.assertIs(command.source, CommandSource.REMOTE_SCADA)
        self.assertEqual(command.value, 62.5)
        self.assertEqual(command.command_id, "cmd-1")
        self.assertEqual(
            bridge._client.messages,
            [
                (
                    "v1/devices/me/rpc/response/42",
                    {"command_id": "cmd-1", "accepted": True, "message": "Accepted"},
                    1,
                )
            ],
        )

    def test_rpc_cannot_directly_control_manual_outputs(self):
        bridge = _bridge()
        message = SimpleNamespace(
            topic="v1/devices/me/rpc/request/43",
            payload=json.dumps(
                {"method": "SET_HEATER", "params": {"value": True}}
            ).encode(),
        )

        bridge._on_message(None, None, message)

        self.assertTrue(bridge._pending.empty())
        self.assertEqual(bridge.controller.commands, [])
        self.assertEqual(bridge._client.messages[0][0], "v1/devices/me/rpc/response/43")
        self.assertIs(bridge._client.messages[0][1]["accepted"], False)
