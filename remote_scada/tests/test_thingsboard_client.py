from __future__ import annotations

import json
import threading
from unittest.mock import Mock
from unittest import TestCase

from remote_scada.thingsboard_client import ThingsBoardClient


class TestThingsBoardClient(TestCase):
    def test_requires_https_for_thingsboard_credentials(self):
        with self.assertRaises(ValueError):
            ThingsBoardClient("http://thingsboard.local", "user", "pass", "device")

    def test_history_downsamples_numeric_series_and_keeps_alarm_events(self):
        client = ThingsBoardClient(
            "https://thingsboard.example", "user", "pass", "device-id"
        )
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs["params"]))
            if kwargs["params"]["keys"] == "alarm_event":
                return {
                    "alarm_event": [
                        {"ts": 1000, "value": '{"alarm_id":"alarm-1"}'}
                    ]
                }
            return {"temperature_c": [{"ts": 1000, "value": "58.5"}]}

        client._request = fake_request
        history = client.telemetry_history(1000, 86_401_000)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2]["agg"], "AVG")
        self.assertEqual(calls[0][2]["interval"], 172_800)
        self.assertEqual(calls[0][2]["limit"], 500)
        self.assertEqual(calls[0][2]["orderBy"], "ASC")
        self.assertEqual(calls[1][2]["limit"], 1000)
        self.assertEqual(calls[1][2]["agg"], "NONE")
        self.assertEqual(history["temperature_c"][0]["value"], 58.5)
        self.assertEqual(
            history["alarm_event"][0]["value"], {"alarm_id": "alarm-1"}
        )
        client.stop()

    def test_supported_history_windows_stay_within_cloud_aggregation_budget(self):
        client = ThingsBoardClient(
            "https://thingsboard.example", "user", "pass", "device-id"
        )
        self.addCleanup(client.stop)
        for hours in (1, 6, 24, 168):
            for requested_limit in (100, 1000, 10000):
                with self.subTest(hours=hours, requested_limit=requested_limit):
                    client._request = Mock(return_value={})
                    start = 1_000_000
                    end = start + hours * 3_600_000
                    self.assertEqual(client.telemetry_history(start, end, limit=requested_limit), {})
                    numeric, events = client._request.call_args_list
                    params = numeric.kwargs["params"]
                    intervals = (end - start + params["interval"] - 1) // params["interval"]
                    self.assertLessEqual(intervals, 500)
                    self.assertLessEqual(params["limit"], requested_limit)
                    self.assertEqual(events.kwargs["params"]["limit"], requested_limit)
                    self.assertEqual(events.kwargs["params"]["agg"], "NONE")

    def test_websocket_latest_telemetry_updates_cache(self):
        client = ThingsBoardClient.__new__(ThingsBoardClient)
        client._latest = {}
        client._latest_lock = threading.Lock()
        client._on_ws_message(
            None,
            json.dumps(
                {
                    "data": {
                        "temperature_c": [[1234, "61.2"]],
                        "mode": [[1234, "AUTO"]],
                    }
                }
            ),
        )

        self.assertEqual(client._latest["temperature_c"], 61.2)
        self.assertEqual(client._latest["mode"], "AUTO")
        self.assertIn("_received_at", client._latest)

    def test_two_way_rpc_uses_rpc_timeout_and_authorization_header(self):
        client = ThingsBoardClient(
            "https://thingsboard.example", "user", "pass", "device-id",
            rpc_timeout_s=15,
        )
        response = Mock(ok=True)
        response.json.return_value = {"accepted": True}
        client._rpc_http = Mock()
        client._rpc_http.request.return_value = response
        client._get_token = lambda: "jwt-token"

        result = client.send_rpc("START_DRYING", {"command_id": "cmd-1"})

        self.assertEqual(result, {"accepted": True})
        kwargs = client._rpc_http.request.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 15)
        self.assertEqual(kwargs["headers"]["X-Authorization"], "Bearer jwt-token")
        client.stop()

    def test_rpc_unauthorized_retry_preserves_rpc_timeout(self):
        client = ThingsBoardClient(
            "https://thingsboard.example", "user", "pass", "device-id",
            rpc_timeout_s=17,
        )
        unauthorized = Mock(ok=False, status_code=401)
        success = Mock(ok=True)
        success.json.return_value = {"accepted": True}
        client._rpc_http = Mock()
        client._rpc_http.request.side_effect = [unauthorized, success]
        client._get_token = Mock(side_effect=["expired-token", "fresh-token"])

        client.send_rpc("START_DRYING", {"command_id": "cmd-1"})

        self.assertEqual(client._rpc_http.request.call_count, 2)
        first, second = client._rpc_http.request.call_args_list
        self.assertEqual(first.kwargs["timeout"], 17)
        self.assertEqual(second.kwargs["timeout"], 17)
        self.assertEqual(
            second.kwargs["headers"]["X-Authorization"], "Bearer fresh-token"
        )
        client.stop()

    def test_slow_history_or_rpc_does_not_block_latest_rest_telemetry(self):
        for channel in ("history", "rpc"):
            with self.subTest(channel=channel):
                client = ThingsBoardClient(
                    "https://thingsboard.example", "user", "pass", "device-id"
                )
                self.addCleanup(client.stop)
                client._get_token = lambda: "jwt-token"
                entered, release, latest_done = (
                    threading.Event(), threading.Event(), threading.Event()
                )
                failures = []
                latest = []
                blocked_response = Mock(ok=True)
                blocked_response.json.return_value = {}

                def slow_request(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("Test did not release slow request")
                    return blocked_response

                session = client._history_http if channel == "history" else client._rpc_http
                session.request = Mock(side_effect=slow_request)
                response = Mock(ok=True)
                response.json.return_value = {"temperature_c": [{"value": "28.7"}]}
                client._http.request = Mock(return_value=response)

                def run_slow():
                    try:
                        if channel == "history":
                            client.telemetry_history(0, 3_600_000)
                        else:
                            client.send_rpc("START_DRYING", {})
                    except Exception as error:
                        failures.append(error)

                def read_latest():
                    try:
                        latest.append(client.latest_state())
                    except Exception as error:
                        failures.append(error)
                    finally:
                        latest_done.set()

                slow = threading.Thread(target=run_slow)
                fast = threading.Thread(target=read_latest)
                slow.start()
                try:
                    self.assertTrue(entered.wait(1), "Slow request did not start")
                    fast.start()
                    self.assertTrue(latest_done.wait(1), "Telemetry blocked behind slow request")
                    self.assertEqual(latest, [{"temperature_c": 28.7}])
                finally:
                    release.set()
                    slow.join(timeout=3)
                    if fast.ident is not None:
                        fast.join(timeout=3)
                self.assertFalse(slow.is_alive())
                self.assertFalse(fast.is_alive())
                self.assertEqual(failures, [])

    def test_stop_closes_all_http_sessions(self):
        client = ThingsBoardClient(
            "https://thingsboard.example", "user", "pass", "device-id"
        )
        sessions = (client._http, client._history_http, client._rpc_http)
        for session in sessions:
            session.close = Mock()
        client.stop()
        for session in sessions:
            session.close.assert_called_once_with()
