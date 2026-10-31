"""Máy khách REST/WebSocket dùng backend SCADA để giao tiếp với ThingsBoard."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from collections.abc import Mapping
from urllib.parse import quote, urlencode, urlsplit

import requests

logger = logging.getLogger(__name__)

# Giới hạn số khoảng AVG để tránh ThingsBoard từ chối truy vấn quá nhiều khoảng.
MAX_HISTORY_BUCKETS = 500

# Các khóa telemetry backend cần đọc trực tiếp và hiển thị cho SCADA.
TELEMETRY_KEYS = (
    "simulation",
    "temperature_c",
    "temperature_setpoint_c",
    "heater_on",
    "process_fan_on",
    "relay_feedback_heater",
    "relay_feedback_process_fan",
    "relay_connected",
    "auto_phase",
    "drying_elapsed_s",
    "drying_remaining_s",
    "hold_timer_running",
    "drying_duration_s",
    "cooling_stop_temperature_c",
    "minimum_setpoint_c",
    "maximum_setpoint_c",
    "control_deadband_c",
    "high_temperature_limit_c",
    "mode",
    "alarm_code",
    "active_alarm",
    "sensor_connected",
    "modbus_connected",
    "mqtt_connected",
    "timestamp_ms",
    "alarm_event",
)


class ThingsBoardError(RuntimeError):
    """Lỗi yêu cầu ThingsBoard hoặc phản hồi không đúng định dạng mong đợi."""


class ThingsBoardClient:
    """Máy khách backend đọc telemetry, lưu cache RAM và gửi two-way RPC."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        device_id: str,
        *,
        request_timeout_s: float = 10.0,
        rpc_timeout_s: float = 15.0,
    ) -> None:
        """Kiểm tra URL HTTPS và khởi tạo phiên HTTP, cache cùng trạng thái WebSocket."""
        self.base_url = base_url.rstrip("/")
        parsed_url = urlsplit(self.base_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("TB_BASE_URL must be an HTTPS URL without embedded credentials.")
        self.username = username
        self.password = password
        self.device_id = device_id
        self.request_timeout_s = request_timeout_s
        self.rpc_timeout_s = rpc_timeout_s
        self._http = requests.Session()
        self._http_lock = threading.Lock()
        # Mỗi loại yêu cầu có session/khóa riêng: RPC hoặc lịch sử chậm không
        # giữ khóa HTTP của telemetry hiện tại. JWT vẫn dùng chung có khóa.
        self._history_http = requests.Session()
        self._history_http_lock = threading.Lock()
        self._rpc_http = requests.Session()
        self._rpc_http_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at = 0.0
        self._latest: dict[str, object] = {}
        self._latest_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._websocket_thread: threading.Thread | None = None
        self._ws: object | None = None

    def start(self) -> None:
        """Khởi chạy luồng nhận telemetry trực tiếp qua WebSocket ThingsBoard."""
        if self._websocket_thread and self._websocket_thread.is_alive():
            return
        self._stop_event.clear()
        self._websocket_thread = threading.Thread(
            target=self._websocket_loop,
            name="thingsboard-telemetry",
            daemon=True,
        )
        self._websocket_thread.start()

    def stop(self) -> None:
        """Dừng WebSocket và đóng phiên HTTP khi backend kết thúc."""
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.warning("Failed to close ThingsBoard WebSocket.")
        if self._websocket_thread and self._websocket_thread.is_alive():
            self._websocket_thread.join(timeout=5)
        self._http.close()
        self._history_http.close()
        self._rpc_http.close()

    def latest_state(self) -> dict[str, object]:
        """Trả telemetry mới từ cache RAM, hoặc đọc REST khi cache cũ/trống."""
        # Ưu tiên cache WebSocket; REST là phương án dự phòng khi cache đã cũ hoặc rỗng.
        with self._latest_lock:
            cached = dict(self._latest)
        if cached and time.monotonic() - float(cached.get("_received_at", 0)) < 10:
            cached.pop("_received_at", None)
            return cached

        raw = self._request(
            "GET",
            f"/api/plugins/telemetry/DEVICE/{quote(self.device_id, safe='')}"
            "/values/timeseries",
            params={"keys": ",".join(TELEMETRY_KEYS), "limit": 1},
        )
        if not isinstance(raw, Mapping):
            raise ThingsBoardError("ThingsBoard returned invalid latest telemetry.")
        result = {key: self._latest_value(raw.get(key)) for key in TELEMETRY_KEYS}
        return {key: value for key, value in result.items() if value is not None}

    def telemetry_history(
        self, start_ts: int, end_ts: int, *, limit: int = 1000
    ) -> dict[str, list[dict[str, object]]]:
        """Đọc lịch sử nhiệt độ và alarm đã lưu trên ThingsBoard."""
        if (
            type(start_ts) is not int
            or type(end_ts) is not int
            or end_ts <= start_ts
            or type(limit) is not int
            or limit <= 0
        ):
            raise ValueError("Telemetry history timestamps must be an increasing range.")
        bucket_limit = min(limit, MAX_HISTORY_BUCKETS)
        interval = max(1000, (end_ts - start_ts + bucket_limit - 1) // bucket_limit)
        device_path = (
            f"/api/plugins/telemetry/DEVICE/{quote(self.device_id, safe='')}"
            "/values/timeseries"
        )
        numeric = self._request(
            "GET",
            device_path,
            channel="history",
            params={
                "keys": "temperature_c,temperature_setpoint_c",
                "startTs": start_ts,
                "endTs": end_ts,
                "limit": bucket_limit,
                "agg": "AVG",
                "interval": interval,
                "orderBy": "ASC",
            },
        )
        events = self._request(
            "GET",
            device_path,
            channel="history",
            params={
                "keys": "alarm_event",
                "startTs": start_ts,
                "endTs": end_ts,
                "limit": limit,
                "agg": "NONE",
            },
        )
        if not isinstance(numeric, Mapping) or not isinstance(events, Mapping):
            raise ThingsBoardError("ThingsBoard returned invalid telemetry history.")
        result = {
            str(key): self._normalize_series(series)
            for key, series in numeric.items()
            if isinstance(series, list)
        }
        event_series = events.get("alarm_event")
        if isinstance(event_series, list):
            result["alarm_event"] = self._normalize_series(event_series)
        return result

    def send_rpc(self, method: str, params: dict[str, object]) -> object:
        """Gửi two-way RPC qua REST và chờ controller máy 1 trả kết quả."""
        # ThingsBoard chuyển yêu cầu REST này tới máy 1 qua MQTT.
        return self._request(
            "POST",
            f"/api/rpc/twoway/{quote(self.device_id, safe='')}",
            json={"method": method, "params": params},
            timeout=self.rpc_timeout_s,
            channel="rpc",
        )

    def _request(
        self, method: str, path: str, *, channel: str = "telemetry", **kwargs: object
    ) -> object:
        """Gửi REST có JWT, thử làm mới token một lần nếu nhận HTTP 401."""
        headers = dict(kwargs.pop("headers", {}) or {})
        timeout = kwargs.pop("timeout", self.request_timeout_s)
        transports = {
            "telemetry": (self._http, self._http_lock),
            "history": (self._history_http, self._history_http_lock),
            "rpc": (self._rpc_http, self._rpc_http_lock),
        }
        http, http_lock = transports[channel]
        for attempt in range(2):
            token = self._get_token()
            request_headers = dict(headers)
            request_headers["X-Authorization"] = f"Bearer {token}"
            try:
                with http_lock:
                    response = http.request(
                        method,
                        self.base_url + path,
                        headers=request_headers,
                        timeout=timeout,
                        **kwargs,
                    )
            except requests.RequestException as error:
                raise ThingsBoardError(f"ThingsBoard request failed: {error}") from error
            if response.status_code == 401 and attempt == 0:
                self._invalidate_token(token)
                continue
            if not response.ok:
                raise ThingsBoardError(
                    f"ThingsBoard returned HTTP {response.status_code}."
                )
            try:
                return response.json()
            except requests.JSONDecodeError as error:
                raise ThingsBoardError("ThingsBoard returned invalid JSON.") from error
        raise ThingsBoardError("ThingsBoard authentication failed after retry.")

    def _get_token(self) -> str:
        """Dùng JWT còn hạn, làm mới bằng refresh token hoặc đăng nhập lại."""
        with self._token_lock:
            if self._token and time.time() < self._token_expires_at - 30:
                return self._token
            if self._refresh_token:
                try:
                    result = self._login(
                        "/api/auth/token", {"refreshToken": self._refresh_token}
                    )
                except ThingsBoardError:
                    logger.info("ThingsBoard refresh failed; signing in again.")
                else:
                    self._store_tokens(result)
                    token = self._token
                    if token is None:
                        raise ThingsBoardError("Không nhận được JWT sau khi làm mới token.")
                    return token

            result = self._login(
                "/api/auth/login",
                {"username": self.username, "password": self.password},
            )
            self._store_tokens(result)
            token = self._token
            if token is None:
                raise ThingsBoardError("Không nhận được JWT sau khi đăng nhập.")
            return token

    def _login(self, path: str, body: dict[str, str]) -> Mapping[str, object]:
        """Gọi API xác thực ThingsBoard và kiểm tra phản hồi chứa JWT."""
        try:
            with self._http_lock:
                response = self._http.post(
                    self.base_url + path,
                    json=body,
                    timeout=self.request_timeout_s,
                )
        except requests.RequestException as error:
            raise ThingsBoardError(f"ThingsBoard authentication failed: {error}") from error
        if not response.ok:
            raise ThingsBoardError(
                f"ThingsBoard authentication returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except requests.JSONDecodeError as error:
            raise ThingsBoardError("ThingsBoard authentication returned invalid JSON.") from error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("token"), str):
            raise ThingsBoardError("ThingsBoard authentication response has no JWT.")
        return payload

    def _store_tokens(self, payload: Mapping[str, object]) -> None:
        """Lưu JWT và thời hạn trong bộ nhớ của tiến trình backend."""
        token = payload.get("token")
        if not isinstance(token, str):
            raise ThingsBoardError("ThingsBoard authentication response has no JWT.")
        self._token = token
        refresh_token = payload.get("refreshToken")
        self._refresh_token = refresh_token if isinstance(refresh_token, str) else None
        self._token_expires_at = self._jwt_expiry(token)

    def _invalidate_token(self, token: str) -> None:
        """Đánh dấu JWT hiện tại cần được làm mới sau phản hồi không xác thực."""
        with self._token_lock:
            if self._token == token:
                self._token_expires_at = 0

    @staticmethod
    def _jwt_expiry(token: str) -> float:
        """Đọc thời điểm hết hạn từ phần payload của JWT."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
            expiry = claims.get("exp")
            if isinstance(expiry, (int, float)):
                return float(expiry)
        except (
            IndexError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ):
            pass
        return time.time() + 300

    def _websocket_loop(self) -> None:
        """Duy trì kết nối WebSocket và thử kết nối lại khi luồng bị ngắt."""
        try:
            import websocket
        except ImportError:
            logger.exception("Install websocket-client to receive live telemetry.")
            return

        while not self._stop_event.is_set():
            try:
                token = self._get_token()
                query = urlencode({"token": token})
                url = f"{self.base_url.replace('https://', 'wss://').replace('http://', 'ws://')}/api/ws/plugins/telemetry?{query}"
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                )
                self._ws.run_forever(
                    ping_interval=20,
                    ping_timeout=10,
                    sslopt={"cert_reqs": 2},
                )
            except Exception as error:
                logger.warning(
                    "ThingsBoard telemetry WebSocket failed (%s).",
                    type(error).__name__,
                )
            finally:
                self._ws = None
            self._stop_event.wait(5)

    def _on_ws_open(self, ws: object) -> None:
        """Đăng ký nhận các khóa telemetry mới nhất khi WebSocket mở."""
        with self._latest_lock:
            self._latest.clear()
        ws.send(
            json.dumps(
                {
                    "tsSubCmds": [
                        {
                            "entityType": "DEVICE",
                            "entityId": self.device_id,
                            "scope": "LATEST_TELEMETRY",
                            "cmdId": 1,
                            "keys": ",".join(TELEMETRY_KEYS),
                        }
                    ],
                    "attrSubCmds": [],
                    "historyCmds": [],
                }
            )
        )

    def _on_ws_message(self, _ws: object, message: str) -> None:
        """Giải mã bản tin WebSocket và cập nhật cache telemetry trong RAM."""
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid ThingsBoard WebSocket JSON.")
            return
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return
        update: dict[str, object] = {}
        for key, values in data.items():
            if key not in TELEMETRY_KEYS or not isinstance(values, list) or not values:
                continue
            update[key] = self._parse_value(values[-1][1] if isinstance(values[-1], list) and len(values[-1]) > 1 else values[-1])
        if update:
            with self._latest_lock:
                self._latest.update(update)
                self._latest["_received_at"] = time.monotonic()

    def _on_ws_error(self, _ws: object, error: object) -> None:
        """Ghi nhận lỗi WebSocket và đánh dấu JWT hết hạn nếu máy chủ trả 401."""
        if getattr(error, "status_code", None) == 401 and self._token:
            self._invalidate_token(self._token)
        logger.warning(
            "ThingsBoard telemetry WebSocket error (%s).", type(error).__name__
        )

    @staticmethod
    def _on_ws_close(_ws: object, status_code: object, _message: object) -> None:
        """Ghi log mã trạng thái khi ThingsBoard đóng WebSocket."""
        logger.info("ThingsBoard telemetry WebSocket closed (status=%s).", status_code)

    @classmethod
    def _normalize_series(cls, values: list[object]) -> list[dict[str, object]]:
        """Chuẩn hóa các điểm lịch sử thành timestamp số và giá trị đã phân tích."""
        normalized = []
        for item in values:
            if not isinstance(item, Mapping) or not isinstance(item.get("ts"), (int, float)):
                continue
            normalized.append(
                {"ts": int(item["ts"]), "value": cls._parse_value(item.get("value"))}
            )
        return normalized

    @staticmethod
    def _latest_value(values: object) -> object:
        """Lấy và phân tích giá trị mới nhất của một khóa telemetry REST."""
        if isinstance(values, list) and values and isinstance(values[0], Mapping):
            return ThingsBoardClient._parse_value(values[0].get("value"))
        return None

    @staticmethod
    def _parse_value(value: object) -> object:
        """Giải mã JSON nếu giá trị telemetry dạng chuỗi chứa JSON."""
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
