"""Preview phải dùng controller mô phỏng và không mở serial/cloud."""

from unittest import TestCase
from unittest.mock import patch

from control_station.modbus import ModbusRTU
from remote_scada.preview import create_preview_app
from remote_scada.thingsboard_client import ThingsBoardClient


class PreviewTests(TestCase):
    def test_preview_login_recipe_history_and_stop_do_not_use_hardware_or_cloud(self):
        with (
            patch.object(ModbusRTU, "connect", side_effect=AssertionError("COM must stay closed")),
            patch.object(ThingsBoardClient, "start", side_effect=AssertionError("No cloud in preview")),
        ):
            app = create_preview_app()
            plant = app.extensions["preview_plant"]
            try:
                client = app.test_client()
                client.get("/")
                with client.session_transaction() as session:
                    csrf = session["csrf_token"]
                response = client.post("/login", data={"username": "operator", "password": "demo-scada",
                                                       "csrf_token": csrf}, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Lịch sử nền là dữ liệu minh họa".encode(), response.data)
                with client.session_transaction() as session:
                    csrf = session["csrf_token"]
                recipe = {"temperature_setpoint_c": 35, "drying_duration_s": 6,
                          "cooling_stop_temperature_c": 32}
                response = client.post("/api/commands", json={"command_type": "SET_RECIPE", "value": recipe},
                                       headers={"X-CSRF-Token": csrf})
                self.assertTrue(response.json["accepted"])
                state = client.get("/api/state").json["state"]
                self.assertTrue(state["simulation"])
                self.assertFalse(state["mqtt_connected"])
                self.assertEqual(state["temperature_setpoint_c"], 35)
                history = client.get("/api/history?hours=1").json
                self.assertTrue(history["series"]["temperature_c"])
                self.assertGreater(history["aggregation_interval_ms"], 0)
            finally:
                self.assertTrue(plant.stop())
            self.assertFalse(plant._thread.is_alive())
