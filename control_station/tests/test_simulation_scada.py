"""Kiểm tra HMI mô phỏng và SCADA qua cùng luồng telemetry/RPC, không mở COM."""

import json
import queue
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from control_station.configuration import load_configuration
from control_station.hmi.app import _DemoPlant, create_app
from control_station.main import main
from control_station.modbus import ModbusRTU
from control_station.mqtt_client import ThingsBoardMqttClient
from remote_scada.server import create_app as create_scada_app


class InMemoryPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, qos):
        self.messages.append((topic, json.loads(payload)))
        return SimpleNamespace(rc=0)


def simulation_bridge(plant):
    bridge = ThingsBoardMqttClient.__new__(ThingsBoardMqttClient)
    bridge.controller = plant.controller
    bridge.relay = plant.relay
    bridge.simulation = True
    bridge._pending = queue.Queue(maxsize=100)
    bridge._client = InMemoryPublisher()
    bridge._mqtt = SimpleNamespace(MQTT_ERR_SUCCESS=0)
    bridge._connected = True
    bridge._connection_lock = threading.Lock()
    bridge._alarm_lock = threading.Lock()
    bridge._last_alarm_id = None
    bridge._last_alarm = None
    bridge._pending_alarm_events = []
    bridge._last_publish_warning = 0.0
    bridge.start = Mock()
    bridge.stop = Mock()
    return bridge


class InMemoryThingsBoard:
    """Thay đường truyền ThingsBoard, giữ nguyên bộ mã hóa telemetry và RPC."""

    def __init__(self, plant, bridge):
        self.plant = plant
        self.bridge = bridge
        self.queued_before_poll = False

    def latest_state(self):
        self.bridge.publish_telemetry()
        return self.bridge._client.messages[-1][1]["values"]

    def send_rpc(self, method, params):
        message = SimpleNamespace(
            topic="v1/devices/me/rpc/request/test-1",
            payload=json.dumps({"method": method, "params": params}).encode(),
        )
        self.bridge._on_message(None, None, message)
        self.queued_before_poll = not self.bridge._pending.empty()
        self.plant.poll_once()
        return self.bridge._client.messages[-1][1]


class SimulationScadaTests(TestCase):
    def setUp(self):
        self.no_com = patch.object(ModbusRTU, "connect", side_effect=AssertionError("COM opened"))
        self.no_com.start()
        self.addCleanup(self.no_com.stop)
        self.plant = _DemoPlant(load_configuration())
        self.plant.poll_once()
        self.bridge = simulation_bridge(self.plant)
        self.plant.attach_mqtt(self.bridge)
        self.addCleanup(self.plant.stop)
        self.gateway = InMemoryThingsBoard(self.plant, self.bridge)
        self.hmi = create_app(self.plant.controller, simulation=True, relay=self.plant.relay).test_client()
        scada_app = create_scada_app(
            self.gateway,
            scada_username="operator",
            scada_password="simulation-test",
            secret_key="simulation-test-session-key-at-least-32-characters",
        )
        self.scada = scada_app.test_client()
        with self.scada.get("/"):
            pass
        with self.scada.session_transaction() as session:
            token = session["csrf_token"]
        self.scada.post("/login", data={
            "username": "operator", "password": "simulation-test", "csrf_token": token,
        })
        with self.scada.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def remote_command(self, kind, value=None):
        return self.scada.post(
            "/api/commands", json={"command_type": kind, "value": value},
            headers={"X-CSRF-Token": self.csrf},
        )

    def test_scada_starts_simulated_batch_and_hmi_sees_same_controller(self):
        result = self.remote_command("START_DRYING")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()["accepted"])
        self.assertTrue(self.gateway.queued_before_poll)
        hmi_state = self.hmi.get("/api/state").get_json()
        self.assertTrue(hmi_state["simulation"])
        self.assertEqual(hmi_state["state"]["mode"], "AUTO")
        self.assertTrue(hmi_state["state"]["heater_on"])
        before = hmi_state["state"]["temperature_c"]
        self.plant.poll_once(1.0)
        scada_state = self.scada.get("/api/state").get_json()["state"]
        self.assertTrue(scada_state["simulation"])
        self.assertTrue(scada_state["mqtt_connected"])
        self.assertGreater(scada_state["temperature_c"], before)
        self.assertEqual(self.remote_command("SET_MODE", "OFF").status_code, 200)
        self.assertFalse(self.plant.relay.heater_on)
        self.assertFalse(self.plant.relay.process_fan_on)

    def test_hmi_setting_reaches_scada_and_scada_setting_reaches_hmi(self):
        result = self.hmi.post("/api/commands", json={"command_type": "SET_TEMPERATURE", "value": 45})
        self.assertEqual(result.status_code, 200)
        state = self.scada.get("/api/state").get_json()["state"]
        self.assertEqual(state["temperature_setpoint_c"], 45)
        self.assertEqual(self.remote_command("SET_TEMPERATURE", 50).status_code, 200)
        settings = self.hmi.get("/api/state").get_json()["settings"]
        self.assertEqual(settings["temperature_setpoint_c"], 50)

    def test_remote_emergency_stop_and_local_hmi_reset(self):
        self.remote_command("START_DRYING")
        self.assertEqual(self.remote_command("EMERGENCY_STOP").status_code, 200)
        self.assertEqual(self.plant.controller.get_state_snapshot().mode.value, "FAULT")
        self.assertFalse(self.plant.relay.heater_on)
        self.assertEqual(self.remote_command("RESET_ALARM").status_code, 400)
        result = self.hmi.post("/api/commands", json={"command_type": "RESET_ALARM"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.plant.controller.get_state_snapshot().mode.value, "OFF")

    def test_recipe_is_applied_atomically_across_scada_mqtt_and_controller(self):
        recipe = {"temperature_setpoint_c": 35, "drying_duration_s": 6,
                  "cooling_stop_temperature_c": 32}
        # Setpoint 35 riêng lẻ không hợp lệ với ngưỡng nguội cũ 35.
        self.assertEqual(self.remote_command("SET_TEMPERATURE", 35).status_code, 409)
        result = self.remote_command("SET_RECIPE", recipe)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.gateway.queued_before_poll)
        state = self.scada.get("/api/state").get_json()["state"]
        for key, value in recipe.items():
            self.assertEqual(state[key], value)
        self.assertFalse(self.plant.relay.heater_on)
        self.assertEqual(state["minimum_setpoint_c"], self.plant.config.control.minimum_setpoint_c)

    def test_invalid_recipe_leaves_every_setting_unchanged_and_active_batch_locks_recipe(self):
        before = self.plant.controller.get_settings_snapshot()
        invalid = {"temperature_setpoint_c": 35, "drying_duration_s": 6,
                   "cooling_stop_temperature_c": 34}
        self.assertEqual(self.remote_command("SET_RECIPE", invalid).status_code, 409)
        self.assertEqual(self.plant.controller.get_settings_snapshot(), before)
        self.remote_command("START_DRYING")
        valid = {**invalid, "cooling_stop_temperature_c": 32}
        self.assertEqual(self.remote_command("SET_RECIPE", valid).status_code, 409)
        self.assertEqual(self.plant.controller.get_settings_snapshot(), before)

    def test_malformed_recipe_is_rejected_before_rpc(self):
        valid = {"temperature_setpoint_c": 35, "drying_duration_s": 6,
                 "cooling_stop_temperature_c": 32}
        for value in ({}, [], {**valid, "drying_duration_s": True},
                      {**valid, "drying_duration_s": 0}, {**valid, "extra": 1},
                      {**valid, "cooling_stop_temperature_c": -1}):
            with self.subTest(value=value):
                self.assertEqual(self.remote_command("SET_RECIPE", value).status_code, 400)
        self.assertFalse(self.gateway.queued_before_poll)

    def test_controller_revalidates_recipe_even_when_mqtt_bypasses_scada_validation(self):
        before = self.plant.controller.get_settings_snapshot()
        valid = {"temperature_setpoint_c": 35, "drying_duration_s": 6,
                 "cooling_stop_temperature_c": 32}
        for value in ({**valid, "drying_duration_s": True},
                      {**valid, "temperature_setpoint_c": float("nan")},
                      {**valid, "extra": 1}):
            with self.subTest(value=value):
                message = SimpleNamespace(
                    topic="v1/devices/me/rpc/request/direct-recipe",
                    payload=json.dumps({"method": "SET_RECIPE", "params": {"value": value}}).encode(),
                )
                self.bridge._on_message(None, None, message)
                self.plant.poll_once()
                response = [payload for topic, payload in self.bridge._client.messages
                            if topic.endswith("/direct-recipe")][-1]
                self.assertFalse(response["accepted"])
                self.assertEqual(self.plant.controller.get_settings_snapshot(), before)

    def test_cloud_failure_keeps_simulation_running_and_stop_closes_bridge(self):
        self.remote_command("START_DRYING")
        self.bridge._connected = False
        self.bridge.process_pending_commands = Mock(side_effect=OSError("cloud unavailable"))
        self.bridge.observe_alarm = Mock(side_effect=OSError("cloud unavailable"))
        with self.assertLogs("control_station.hmi.app", level="ERROR"):
            self.plant.poll_once(1.0)
        state = self.plant.controller.get_state_snapshot()
        self.assertEqual(state.mode.value, "AUTO")
        self.assertFalse(state.mqtt_connected)
        self.assertTrue(self.plant.stop())
        self.bridge.stop.assert_called_once()


class SimulationCommandLineTests(TestCase):
    def test_mqtt_simulation_starts_bridge_without_serial_port(self):
        with patch("control_station.main.load_dotenv"), patch.dict("os.environ", {
            "TB_DEVICE_TOKEN": "simulation-device-token", "TB_MQTT_PORT": "8883",
            "TB_MQTT_HOST": "test-broker.example",
        }, clear=True), patch("control_station.mqtt_client.ThingsBoardMqttClient") as factory, \
                patch("flask.Flask.run"), patch.object(ModbusRTU, "connect", side_effect=AssertionError("COM opened")):
            self.assertEqual(main(["--mqtt"]), 0)
            self.assertTrue(factory.call_args.kwargs["simulation"])
            factory.return_value.start.assert_called_once()
            factory.return_value.stop.assert_called_once()

    def test_default_simulation_does_not_connect_cloud_even_with_token(self):
        with patch("control_station.main.load_dotenv"), patch.dict("os.environ", {
            "TB_DEVICE_TOKEN": "simulation-device-token",
        }, clear=True), patch("control_station.mqtt_client.ThingsBoardMqttClient") as factory, \
                patch("flask.Flask.run"):
            self.assertEqual(main([]), 0)
            factory.assert_not_called()

    def test_mqtt_flag_requires_device_token_before_starting_simulation(self):
        with patch("control_station.main.load_dotenv"), patch.dict("os.environ", {}, clear=True), \
                patch("control_station.main.create_demo_app") as create_demo, \
                self.assertRaises(SystemExit) as result:
            main(["--mqtt"])
        self.assertEqual(result.exception.code, 2)
        create_demo.assert_not_called()
