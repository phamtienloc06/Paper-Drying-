from __future__ import annotations

from unittest import TestCase

from remote_scada.server import create_app


class FakeScadaGateway:
    def __init__(self) -> None:
        self.calls = []
        self.accepted = True

    def latest_state(self):
        return {
            "temperature_c": 58.4,
            "temperature_setpoint_c": 60.0,
            "heater_on": False,
            "process_fan_on": True,
            "relay_feedback_heater": False,
            "relay_feedback_process_fan": True,
            "relay_connected": True,
            "auto_phase": "HOLDING",
            "drying_elapsed_s": 240.0,
            "drying_remaining_s": 360.0,
            "hold_timer_running": True,
            "drying_duration_s": 600.0,
            "cooling_stop_temperature_c": 35.0,
            "mode": "AUTO",
            "alarm_code": "NONE",
            "active_alarm": None,
            "sensor_connected": True,
            "modbus_connected": True,
            "mqtt_connected": True,
            "timestamp_ms": 1_800_000_000_000,
        }

    def telemetry_history(self, start_ts, end_ts):
        return {
            "temperature_c": [
                {"ts": start_ts, "value": 55.0},
                {"ts": end_ts, "value": 58.4},
            ],
            "temperature_setpoint_c": [{"ts": end_ts, "value": 60.0}],
            "alarm_event": [],
        }

    def send_rpc(self, method, params):
        self.calls.append((method, params))
        return {
            "command_id": params["command_id"],
            "accepted": self.accepted,
            "message": "Accepted" if self.accepted else "Rejected by controller",
        }


class ScadaEndToEndTests(TestCase):
    def setUp(self):
        self.gateway = FakeScadaGateway()
        self.app = create_app(
            self.gateway,
            scada_username="operator",
            scada_password="local-test-password",
            secret_key="long-random-test-session-key-for-scada",
        )
        self.app.testing = True
        self.client = self.app.test_client()

    def login(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="password"', page.data)
        with self.client.session_transaction() as current_session:
            csrf_token = current_session["csrf_token"]

        response = self.client.post(
            "/login",
            data={
                "username": "operator",
                "password": "local-test-password",
                "csrf_token": csrf_token,
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.client.session_transaction() as current_session:
            return current_session["csrf_token"]

    def test_operator_can_sign_in_view_live_dashboard_and_load_chart_history(self):
        self.login()

        dashboard = self.client.get("/")
        state = self.client.get("/api/state")
        history = self.client.get("/api/history?hours=24")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Tr\xe1\xba\xa1ng th\xc3\xa1i h\xe1\xbb\x87 th\xe1\xbb\x91ng", dashboard.data)
        self.assertIn(b"/static/app.js", dashboard.data)
        self.assertEqual(state.json["state"]["temperature_c"], 58.4)
        self.assertEqual(state.json["state"]["mode"], "AUTO")
        self.assertEqual(history.json["series"]["temperature_c"][-1]["value"], 58.4)

    def test_remote_operator_can_issue_only_high_level_commands(self):
        csrf_token = self.login()
        response = self.client.post(
            "/api/commands",
            json={"command_type": "START_DRYING"},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        method, params = self.gateway.calls[0]
        self.assertEqual(method, "START_DRYING")
        self.assertIsNone(params.get("value"))
        self.assertIsInstance(params["command_id"], str)
        self.assertIsInstance(params["timestamp_ms"], int)

        forbidden = self.client.post(
            "/api/commands",
            json={"command_type": "SET_PROCESS_FAN", "value": True},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(forbidden.status_code, 400)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_dashboard_displays_controller_rejection_without_claiming_success(self):
        csrf_token = self.login()
        self.gateway.accepted = False

        response = self.client.post(
            "/api/commands",
            json={"command_type": "SET_MODE", "value": "OFF"},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["accepted"])
        self.assertEqual(response.json["message"], "Rejected by controller")

    def test_mutating_api_requires_csrf_even_after_login(self):
        self.login()

        response = self.client.post(
            "/api/commands",
            json={"command_type": "EMERGENCY_STOP"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.gateway.calls, [])
