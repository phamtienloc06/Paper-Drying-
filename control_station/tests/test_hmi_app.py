"""Kiểm thử HTTP contract của HMI không cần phần cứng."""

from __future__ import annotations

import unittest
from control_station.tests.helpers import make_settings

from control_station.controller import SystemController
from control_station.hmi.app import create_app
from control_station.models import SystemState


class FakeRelay:
    def __init__(self) -> None:
        self.heater_on = False
        self.process_fan_on = False
        self.communication_status = True

    def set_heater(self, enabled: bool) -> None:
        self.heater_on = enabled

    def set_process_fan(self, enabled: bool) -> None:
        self.process_fan_on = enabled

    def all_outputs_off(self) -> None:
        self.heater_on = False
        self.process_fan_on = False

    @property
    def last_known_state(self):
        return {"heater": self.heater_on, "process_fan": self.process_fan_on}


class TestHmiApi(unittest.TestCase):
    def setUp(self) -> None:
        self.relay = FakeRelay()
        self.controller = SystemController(SystemState(), make_settings(), self.relay)
        self.controller.update_measurement(32.5, True, True)
        self.controller.run_control_cycle()
        app = create_app(self.controller, relay=self.relay)
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def command(self, command_type: str, value=None):
        return self.client.post(
            "/api/commands",
            json={
                "command_type": command_type,
                "value": value,
            },
        )

    def test_index_and_static_assets_are_served(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Buồng sấy giấy", response.get_data(as_text=True))
        with self.client.get("/static/app.js") as script_response:
            self.assertEqual(script_response.status_code, 200)
        with self.client.get("/static/style.css") as style_response:
            self.assertEqual(style_response.status_code, 200)

    def test_state_endpoint_returns_snapshots(self) -> None:
        response = self.client.get("/api/state")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["state"]["temperature_c"], 32.5)
        self.assertEqual(payload["state"]["mode"], "OFF")
        self.assertEqual(payload["settings"]["temperature_setpoint_c"], 60.0)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_hmi_can_change_mode_and_control_manual_output(self) -> None:
        response = self.command("SET_MODE", "MANUAL")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["accepted"])

        response = self.command("SET_HEATER", True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.relay.heater_on)
        self.assertFalse(self.relay.process_fan_on)

    def test_setpoint_is_validated_by_controller(self) -> None:
        self.assertEqual(self.command("SET_TEMPERATURE", 45.0).status_code, 200)
        rejected = self.command("SET_TEMPERATURE", 200.0)
        self.assertEqual(rejected.status_code, 409)
        self.assertFalse(rejected.get_json()["accepted"])
        self.assertEqual(
            self.controller.get_settings_snapshot().temperature_setpoint_c, 45.0
        )

    def test_unknown_command_and_non_object_body_return_400(self) -> None:
        self.assertEqual(self.command("DELETE_EVERYTHING").status_code, 400)
        response = self.client.post(
            "/api/commands",
            data="[]",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_emergency_stop_is_available_from_local_hmi(self) -> None:
        self.command("SET_MODE", "MANUAL")
        self.command("SET_HEATER", True)
        response = self.command("EMERGENCY_STOP")
        state = self.client.get("/api/state").get_json()["state"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state["mode"], "FAULT")
        self.assertEqual(state["alarm_code"], "EMERGENCY_STOPPED")
        self.assertFalse(self.relay.heater_on)
        self.assertFalse(self.relay.process_fan_on)

    def test_feedback_changes_only_after_accepted_manual_command(self):
        self.command("SET_MODE", "AUTO")
        self.assertEqual(self.command("SET_HEATER", True).status_code, 409)
        self.assertFalse(self.client.get("/api/state").get_json()["relay_feedback"]["heater"])
        self.command("SET_MODE", "MANUAL")
        self.command("SET_HEATER", True)
        self.assertTrue(self.client.get("/api/state").get_json()["relay_feedback"]["heater"])

    def test_unknown_or_failed_relay_has_no_confirmed_output_feedback(self):
        for status in (None, False):
            self.relay.communication_status = status
            payload = self.client.get("/api/state").get_json()
            self.assertIs(payload["state"]["relay_connected"], status)
            self.assertEqual(payload["relay_feedback"], {"heater": None, "process_fan": None})


if __name__ == "__main__":
    unittest.main()
