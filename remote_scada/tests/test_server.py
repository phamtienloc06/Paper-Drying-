from __future__ import annotations

from unittest import TestCase

from remote_scada.server import create_app


class FakeThingsBoard:
    def __init__(self) -> None:
        self.commands = []

    def latest_state(self):
        return {"temperature_c": 57.2, "mode": "AUTO"}

    def telemetry_history(self, start_ts, end_ts):
        return {
            "temperature_c": [{"ts": start_ts, "value": 55.0}],
            "alarm_event": [
                {
                    "ts": start_ts,
                    "value": [
                        {
                            "alarm_id": "a-1",
                            "code": "OVER_TEMPERATURE",
                            "severity": "CRITICAL",
                            "message": "High temperature",
                            "raised_at_ms": start_ts,
                        }
                    ],
                }
            ],
        }

    def send_rpc(self, method, params):
        self.commands.append((method, params))
        return {
            "command_id": params["command_id"],
            "accepted": True,
            "message": "Accepted",
        }


def _client():
    thingsboard = FakeThingsBoard()
    app = create_app(
        thingsboard,
        scada_username="operator",
        scada_password="secret",
        secret_key="test-secret-key-for-scada-session-testing",
    )
    app.testing = True
    return app.test_client(), thingsboard


def _login(client):
    client.get("/")
    with client.session_transaction() as current_session:
        csrf_token = current_session["csrf_token"]
    response = client.post(
        "/login",
        data={
            "username": "operator",
            "password": "secret",
            "csrf_token": csrf_token,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Tr\xe1\xba\xa1ng th\xc3\xa1i h\xe1\xbb\x87 th\xe1\xbb\x91ng" in response.data


class TestScadaServer(TestCase):
    def test_data_apis_require_login(self):
        client, _ = _client()
        self.assertEqual(client.get("/api/state").status_code, 401)
        self.assertEqual(client.get("/api/history").status_code, 401)
        self.assertEqual(client.post("/api/commands", json={}).status_code, 401)

    def test_login_rejects_bad_credentials_and_requires_csrf(self):
        client, _ = _client()
        client.get("/")
        with client.session_transaction() as current_session:
            csrf_token = current_session["csrf_token"]
        no_csrf = client.post(
            "/login", data={"username": "operator", "password": "secret"}
        )
        self.assertEqual(no_csrf.status_code, 400)
        invalid = client.post(
            "/login",
            data={
                "username": "operator",
                "password": "wrong",
                "csrf_token": csrf_token,
            },
        )
        self.assertEqual(invalid.status_code, 401)

    def test_authenticated_state_and_history(self):
        client, _ = _client()
        _login(client)
        state = client.get("/api/state")
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json["state"]["temperature_c"], 57.2)

        history = client.get("/api/history?hours=1")
        self.assertEqual(history.status_code, 200)
        self.assertTrue(history.json["series"]["temperature_c"])
        self.assertEqual(history.json["alarm_events"][0]["alarm_id"], "a-1")
        self.assertEqual(client.get("/api/history?hours=2").status_code, 400)

    def test_remote_commands_are_whitelisted_and_forwarded(self):
        client, thingsboard = _client()
        _login(client)
        with client.session_transaction() as current_session:
            csrf_token = current_session["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        accepted = client.post(
            "/api/commands",
            json={"command_type": "SET_DRYING_DURATION", "value": 600},
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 200)
        method, params = thingsboard.commands[-1]
        self.assertEqual(method, "SET_DRYING_DURATION")
        self.assertEqual(params["value"], 600)
        self.assertGreater(params["timestamp_ms"], 0)
        self.assertEqual(accepted.json["command_id"], params["command_id"])

        for command in (
            {"command_type": "SET_MODE", "value": "MANUAL"},
            {"command_type": "SET_HEATER", "value": True},
            {"command_type": "RESET_ALARM"},
            {"command_type": "SET_TEMPERATURE", "value": True},
            {"command_type": "SET_DRYING_DURATION", "value": 0},
        ):
            response = client.post("/api/commands", json=command, headers=headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(len(thingsboard.commands), 1)

    def test_emergency_stop_requires_post_and_sends_allowed_rpc(self):
        client, thingsboard = _client()
        _login(client)
        with client.session_transaction() as current_session:
            csrf_token = current_session["csrf_token"]
        response = client.post(
            "/api/commands",
            json={"command_type": "EMERGENCY_STOP"},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(thingsboard.commands[0][0], "EMERGENCY_STOP")
        self.assertNotIn("value", thingsboard.commands[0][1])
