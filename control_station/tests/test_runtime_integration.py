"""Kiểm tra cấu hình–driver–controller–HMI bằng bus giả và đồng hồ giả."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import yaml

from control_station.configuration import DEFAULT_CONFIG_PATH, load_configuration
from control_station.hmi.app import create_app, create_demo_app
from control_station.modbus import ModbusCommunicationError
from control_station.models import AlarmCode, AutoPhase, ControlMode
from control_station.runtime import StationRuntime


class FakeBus:
    def __init__(self):
        self.raw = 280
        self.coils = [False] * 8  # Bit phản hồi OFF của cuộn relay.
        self.read_error = False
        self.ignore_fan = False
        self.connect_calls = 0
        self.close_calls = 0
        self.writes = []
        self.coil_read_calls = []
        self.coil_read_retries = []
        self.relay_read_error = False

    def connect(self):
        self.connect_calls += 1

    def close(self):
        self.close_calls += 1

    def read_holding_registers(self, slave_id, address, count=1):
        if self.read_error:
            raise ModbusCommunicationError("sensor timeout")
        return [self.raw]

    def write_coil(self, slave_id, address, enabled):
        self.writes.append((address, enabled))
        if not (self.ignore_fan and address == 1 and enabled):
            self.coils[address] = enabled

    def read_coils(self, slave_id, address, count=1, *, retries=None):
        self.coil_read_calls.append((slave_id, address, count))
        self.coil_read_retries.append(retries)
        if self.relay_read_error:
            raise ModbusCommunicationError("relay timeout")
        return self.coils[address : address + count]


class RuntimeIntegrationTests(unittest.TestCase):
    def configuration(self, mutate=lambda data: None):
        data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
        mutate(data)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            return load_configuration(path)

    def setup_runtime(self, config=None):
        self.now = 100.0
        self.bus = FakeBus()
        self.runtime = StationRuntime(
            config or load_configuration(), bus=self.bus, clock=lambda: self.now
        )
        self.runtime.poll_once()
        self.client = create_app(self.runtime.controller, relay=self.runtime.relay).test_client()

    def command(self, kind, value=None):
        return self.client.post(
            "/api/commands", json={"command_type": kind, "value": value}
        )

    def test_yaml_controls_actual_auto_timers_and_hmi_payload(self):
        config = self.configuration(
            lambda d: d["control"].update(
                temperature_setpoint_c=50,
                heater_to_fan_start_delay_s=3,
                drying_duration_s=7,
            )
        )
        self.setup_runtime(config)
        self.assertEqual(self.command("SET_MODE", "AUTO").status_code, 200)
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [True, False])
        self.now += 2.9
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [True, False])
        self.now += 0.1
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [True, True])
        self.bus.raw = 500
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [False, True])
        self.now += 6.9
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [False, True])
        self.now += 0.1
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [False, True])
        self.assertEqual(
            self.runtime.controller.get_state_snapshot().auto_phase, AutoPhase.COOLING
        )
        self.bus.raw = 350
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [False, False])
        payload = self.client.get("/api/state").get_json()
        self.assertEqual(payload["settings"]["drying_duration_s"], 7)
        self.assertEqual(payload["state"]["auto_phase"], "COMPLETE")
        self.assertIn("process_fan_on", payload["state"])
        self.assertNotIn("cooling_fan_on", payload["state"])

    def test_manual_hmi_can_run_both_loads_and_emergency_stop_cancels_them(self):
        self.setup_runtime()
        self.assertEqual(self.command("SET_MODE", "MANUAL").status_code, 200)
        self.assertEqual(self.command("SET_HEATER", True).status_code, 200)
        self.assertEqual(self.command("SET_PROCESS_FAN", True).status_code, 200)
        self.assertEqual(self.bus.coils[:2], [True, True])
        self.assertEqual(self.command("EMERGENCY_STOP").status_code, 200)
        self.assertEqual(self.bus.coils[:2], [False, False])

    def test_relay_probe_status_is_separate_from_healthy_pt100_and_bus(self):
        self.setup_runtime()
        self.bus.relay_read_error = True
        self.now += 1
        self.runtime.poll_once()
        payload = self.client.get("/api/state").get_json()
        self.assertTrue(payload["state"]["sensor_connected"])
        self.assertTrue(payload["state"]["modbus_connected"])
        self.assertFalse(payload["state"]["relay_connected"])
        self.assertEqual(payload["relay_feedback"], {"heater": None, "process_fan": None})
        self.assertEqual(payload["state"]["mode"], "OFF")
        self.bus.relay_read_error = False
        self.now += 1
        self.runtime.poll_once()
        self.assertTrue(self.client.get("/api/state").get_json()["state"]["relay_connected"])

    def test_hmi_gets_cached_relay_feedback_without_serial_io(self):
        self.setup_runtime()
        reads = len(self.bus.coil_read_calls)
        for _ in range(3):
            payload = self.client.get("/api/state").get_json()
            self.assertEqual(payload["relay_feedback"], {"heater": False, "process_fan": False})
        self.assertEqual(len(self.bus.coil_read_calls), reads)
        self.now += .5
        self.runtime.poll_once()
        self.assertEqual(len(self.bus.coil_read_calls), reads)
        self.now += .5
        self.runtime.poll_once()
        self.assertEqual(len(self.bus.coil_read_calls), reads + 1)

    def test_periodic_relay_probe_does_not_retry_but_command_readback_keeps_default(self):
        self.setup_runtime()
        # Hai lần xác nhận OFF lúc khởi động dùng chính sách mặc định của bus.
        self.assertEqual(self.bus.coil_read_retries, [None, None, 0])
        self.bus.relay_read_error = True
        self.now += 1
        self.runtime.poll_once()
        self.assertEqual(self.bus.coil_read_retries[-1], 0)
        self.assertTrue(self.runtime.controller.get_state_snapshot().sensor_connected)
        self.assertFalse(self.runtime.relay.communication_status)

    def test_control_period_includes_io_time_and_skips_missed_ticks(self):
        now = [0.0]
        runtime = StationRuntime(load_configuration(), bus=FakeBus(), clock=lambda: now[0])
        starts, waits = [], []
        work = [0.04, 0.35, 0.04]

        def wait(delay):
            waits.append(delay)
            if len(starts) == len(work):
                return True
            now[0] += delay
            return False

        def poll_once():
            starts.append(now[0])
            now[0] += work[len(starts) - 1]

        runtime._stop_event = Mock()
        runtime._stop_event.wait.side_effect = wait
        runtime.poll_once = poll_once
        runtime._run()
        for actual, expected in zip(waits, [0.2, 0.16, 0.0, 0.16], strict=True):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(starts, [0.2, 0.4, 0.75], strict=True):
            self.assertAlmostEqual(actual, expected)

    def test_shutdown_confirms_outputs_off_and_closes_bus_before_cloud_stop(self):
        self.setup_runtime()
        self.command("SET_MODE", "MANUAL")
        self.command("SET_HEATER", True)

        def stop_cloud():
            self.assertEqual(self.bus.coils[:2], [False, False])
            self.assertEqual(self.bus.close_calls, 1)

        runtime_cloud = Mock()
        runtime_cloud.stop.side_effect = stop_cloud
        self.runtime.mqtt_client = runtime_cloud
        self.assertTrue(self.runtime.stop())
        runtime_cloud.stop.assert_called_once_with()

    def test_stop_during_sensor_read_skips_queued_commands_and_relay_monitoring(self):
        self.setup_runtime()
        cloud = Mock()
        self.runtime.mqtt_client = cloud
        reads = len(self.bus.coil_read_calls)

        def read_temperature():
            self.runtime._stop_event.set()
            return 28.0

        self.runtime.sensor.read_temperature = read_temperature
        self.runtime.poll_once()
        cloud.process_pending_commands.assert_not_called()
        self.assertEqual(len(self.bus.coil_read_calls), reads)

    def test_relay_feedback_uses_actual_readback_not_commanded_state(self):
        self.setup_runtime()
        self.bus.coils = [False] * 8
        self.now += 1
        self.runtime.poll_once()
        payload = self.client.get("/api/state").get_json()
        self.assertFalse(payload["state"]["heater_on"])
        self.assertEqual(payload["relay_feedback"], {"heater": False, "process_fan": False})

    def test_bus_loss_clears_relay_status_and_feedback(self):
        self.setup_runtime()
        self.bus.read_error = True
        self.runtime.poll_once()
        payload = self.client.get("/api/state").get_json()
        self.assertFalse(payload["state"]["relay_connected"])
        self.assertEqual(payload["relay_feedback"], {"heater": None, "process_fan": None})

    def test_hmi_recipe_pause_resume_and_complete_through_real_drivers(self):
        self.setup_runtime(
            self.configuration(lambda d: d["control"].update(temperature_setpoint_c=50))
        )
        self.assertEqual(self.command("SET_DRYING_DURATION", 120).status_code, 200)
        self.assertEqual(self.command("START_DRYING").status_code, 200)
        self.runtime.poll_once()
        self.now += 5
        self.bus.raw = 500
        self.runtime.poll_once()
        self.now += 30
        self.runtime.poll_once()
        self.now += 40
        self.bus.raw = 470
        self.runtime.poll_once()
        payload = self.client.get("/api/state").get_json()
        self.assertEqual(payload["state"]["drying_elapsed_s"], 30)
        self.assertFalse(payload["state"]["hold_timer_running"])
        self.assertEqual(self.bus.coils[:2], [True, True])
        self.now += 20
        self.bus.raw = 500
        self.runtime.poll_once()
        self.assertEqual(self.command("SET_DRYING_DURATION", 60).status_code, 409)
        self.now += 90
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [False, True])
        self.assertEqual(
            self.runtime.controller.get_state_snapshot().auto_phase, AutoPhase.COOLING
        )
        self.bus.raw = 350
        self.runtime.poll_once()
        payload = self.client.get("/api/state").get_json()
        self.assertEqual(payload["state"]["auto_phase"], "COMPLETE")
        self.assertEqual(payload["state"]["drying_remaining_s"], 0)
        self.assertEqual(self.bus.coils[:2], [False, False])

    def test_bus_loss_then_reconnect_stays_fault_and_synchronizes_off(self):
        self.setup_runtime()
        self.command("SET_MODE", "AUTO")
        self.runtime.poll_once()
        self.bus.read_error = True
        self.runtime.poll_once()
        state = self.runtime.controller.get_state_snapshot()
        self.assertEqual(state.mode, ControlMode.FAULT)
        self.assertIsNone(state.temperature_c)
        self.bus.read_error = False
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [False, False])
        self.assertEqual(
            self.runtime.controller.get_state_snapshot().mode, ControlMode.FAULT
        )
        self.assertEqual(self.command("RESET_ALARM").status_code, 200)
        self.assertEqual(
            self.runtime.controller.get_state_snapshot().mode, ControlMode.OFF
        )

    def test_invalid_sensor_sample_stops_heater_and_requests_fan(self):
        self.setup_runtime()
        self.command("SET_MODE", "AUTO")
        self.runtime.poll_once()
        self.bus.raw = 8501
        self.runtime.poll_once()
        self.assertEqual(self.bus.coils[:2], [False, True])
        self.assertEqual(
            self.runtime.controller.get_state_snapshot().alarm_code,
            AlarmCode.SENSOR_DISCONNECTED,
        )

    def test_real_driver_fan_readback_failure_latches_fault(self):
        self.setup_runtime()
        self.command("SET_MODE", "AUTO")
        self.runtime.poll_once()
        self.bus.ignore_fan = True
        self.now += self.runtime.config.control.heater_to_fan_start_delay_s
        self.runtime.poll_once()
        state = self.runtime.controller.get_state_snapshot()
        self.assertEqual(state.alarm_code, AlarmCode.PROCESS_FAN_START_FAILED)
        self.assertEqual(self.bus.coils[:2], [False, False])

    def test_shutdown_during_auto_disables_loads_and_closes_bus(self):
        self.setup_runtime()
        self.command("SET_MODE", "AUTO")
        self.runtime.poll_once()
        self.now += 5
        self.runtime.poll_once()
        self.assertTrue(self.runtime.stop())
        self.assertEqual(self.bus.coils[:2], [False, False])
        self.assertEqual(self.bus.close_calls, 1)
        state = self.runtime.controller.get_state_snapshot()
        self.assertEqual(state.mode, ControlMode.OFF)
        self.assertEqual(state.auto_phase, AutoPhase.IDLE)

    def test_nc_startup_and_shutdown_use_energized_coils_to_disable_loads(self):
        config = load_configuration()
        relay = deepcopy(config.relay)
        for output in relay["outputs"].values():
            output["contact"] = "NC"
        self.setup_runtime(replace(config, relay=relay))
        self.assertEqual(self.bus.coils[:2], [True, True])
        self.command("SET_MODE", "MANUAL")
        self.command("SET_HEATER", True)
        self.assertEqual(self.bus.coils[0], False)
        self.assertTrue(self.runtime.stop())
        self.assertEqual(self.bus.coils[:2], [True, True])

    def test_configuration_rejects_missing_timer_duplicate_ids_and_invalid_settings(
        self,
    ):
        mutations = (
            lambda d: d["control"].pop("drying_duration_s"),
            lambda d: d["control"].update(drying_duration_s=True),
            lambda d: d["control"].update(drying_duration_s=0),
            lambda d: d["control"].pop("cooling_stop_temperature_c"),
            lambda d: d["control"].update(cooling_stop_temperature_c=58),
            lambda d: d["relay"].update(slave_id=d["pt100"]["slave_id"]),
            lambda d: d["control"].update(high_temperature_limit_c=70),
            lambda d: d["relay"]["outputs"]["heater"].update(contact=False),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.configuration(mutate)

    def test_demo_uses_same_yaml_settings_and_stops_its_thread(self):
        config = self.configuration(lambda d: d["control"].update(drying_duration_s=23))
        app = create_demo_app(config)
        plant = app.extensions["demo_plant"]
        try:
            payload = app.test_client().get("/api/state").get_json()
            self.assertTrue(payload["simulation"])
            self.assertEqual(payload["settings"]["drying_duration_s"], 23)
        finally:
            plant.stop()
        self.assertFalse(plant._thread.is_alive())
