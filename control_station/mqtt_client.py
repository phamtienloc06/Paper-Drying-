"""Cầu nối MQTT tùy chọn gửi telemetry và nhận yêu cầu từ ThingsBoard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import json
import logging
import math
import queue
import threading
import time
from uuid import uuid4

from .models import CommandSource, CommandType, ControlCommand

logger = logging.getLogger(__name__)
# Danh sách lệnh cấp cao máy 1 chấp nhận nhận từ ThingsBoard/SCADA.
REMOTE_COMMANDS = {
    CommandType.SET_MODE,
    CommandType.START_DRYING,
    CommandType.SET_TEMPERATURE,
    CommandType.SET_DRYING_DURATION,
    CommandType.SET_COOLING_TEMPERATURE,
    CommandType.SET_RECIPE,
    CommandType.EMERGENCY_STOP,
}


class ThingsBoardMqttClient:
    """Gửi trạng thái lên cloud và chuyển RPC vào vòng điều khiển cục bộ."""

    def __init__(
        self,
        device_token: str,
        controller,
        relay,
        *,
        host: str = "mqtt.thingsboard.cloud",
        port: int = 8883,
        simulation: bool = False,
    ) -> None:
        """Khởi tạo MQTT/TLS bằng Device Token và chuẩn bị hàng đợi lệnh từ xa."""
        if not device_token or device_token.startswith("replace_with"):
            raise ValueError("Set a valid ThingsBoard device token before enabling MQTT.")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("ThingsBoard MQTT host is required.")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("ThingsBoard MQTT port must be between 1 and 65535.")
        self.controller = controller
        self.relay = relay
        self.host = host.strip()
        self.port = port
        self.simulation = simulation
        self._pending: queue.Queue[tuple[str, ControlCommand]] = queue.Queue(maxsize=100)
        self._connected = False
        self._connection_lock = threading.Lock()
        self._alarm_lock = threading.Lock()
        self._last_alarm_id: str | None = None
        self._last_alarm: dict[str, object] | None = None
        self._pending_alarm_events: list[dict[str, object]] = []
        self._last_publish_warning = 0.0
        self._telemetry_stop = threading.Event()
        self._telemetry_thread: threading.Thread | None = None

        try:
            import paho.mqtt.client as mqtt
        except ImportError as error:
            raise RuntimeError(
                "Install paho-mqtt to enable ThingsBoard MQTT."
            ) from error
        self._mqtt = mqtt
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"drying-scada-{uuid4()}",
        )
        self._client.username_pw_set(device_token)
        self._client.tls_set()
        self._client.tls_insecure_set(False)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def connected(self) -> bool:
        """Trả về trạng thái kết nối MQTT hiện tại một cách an toàn giữa các luồng."""
        with self._connection_lock:
            return self._connected

    def start(self) -> None:
        """Kết nối bất đồng bộ tới broker và bắt đầu luồng gửi telemetry."""
        self._telemetry_stop.clear()
        self._client.connect_async(self.host, self.port, keepalive=60)
        self._client.loop_start()
        self._telemetry_thread = threading.Thread(
            target=self._telemetry_loop,
            name="thingsboard-telemetry-publisher",
            daemon=True,
        )
        self._telemetry_thread.start()

    def stop(self) -> None:
        """Dừng luồng telemetry, ngắt MQTT và xóa trạng thái kết nối."""
        self._telemetry_stop.set()
        if self._telemetry_thread and self._telemetry_thread.is_alive():
            self._telemetry_thread.join(timeout=2)
        self._client.disconnect()
        self._client.loop_stop()
        with self._connection_lock:
            self._connected = False

    def process_pending_commands(self, maximum: int = 20) -> None:
        """Chuyển các RPC đang chờ cho controller và gửi kết quả về ThingsBoard."""
        # Chỉ luồng điều khiển gọi hàm này; callback MQTT không trực tiếp thao tác relay.
        for _ in range(maximum):
            try:
                request_id, command = self._pending.get_nowait()
            except queue.Empty:
                return
            try:
                result = self.controller.process_command(command)
                response = {
                    "command_id": result.command_id,
                    "accepted": result.accepted,
                    "message": result.message,
                }
            except Exception as error:
                logger.exception("Remote RPC command %s could not be processed.", request_id)
                response = {
                    "command_id": command.command_id,
                    "accepted": False,
                    "message": f"Controller processing failed: {error}",
                }
            self._publish_rpc_response(request_id, response)

    def publish_telemetry(self) -> None:
        """Gửi snapshot controller và sự kiện cảnh báo lên telemetry ThingsBoard."""
        # Trạng thái phản hồi coil không chứng minh tải vật lý đang hoạt động.
        self.observe_alarm()
        state = self.controller.get_state_snapshot()
        settings = self.controller.get_settings_snapshot()
        alarm = self.controller.get_active_alarm()
        state_data = asdict(state)
        settings_data = asdict(settings)
        values: dict[str, object] = {
            "simulation": self.simulation,
            "temperature_c": state_data["temperature_c"],
            "temperature_setpoint_c": settings_data["temperature_setpoint_c"],
            "heater_on": state_data["heater_on"],
            "process_fan_on": state_data["process_fan_on"],
            "auto_phase": self._enum_value(state_data["auto_phase"]),
            "drying_elapsed_s": state_data["drying_elapsed_s"],
            "drying_remaining_s": state_data["drying_remaining_s"],
            "hold_timer_running": state_data["hold_timer_running"],
            "drying_duration_s": settings_data["drying_duration_s"],
            "cooling_stop_temperature_c": settings_data["cooling_stop_temperature_c"],
            "minimum_setpoint_c": settings_data["minimum_setpoint_c"],
            "maximum_setpoint_c": settings_data["maximum_setpoint_c"],
            "control_deadband_c": settings_data["control_deadband_c"],
            "high_temperature_limit_c": settings_data["high_temperature_limit_c"],
            "mode": self._enum_value(state_data["mode"]),
            "alarm_code": self._enum_value(state_data["alarm_code"]),
            "sensor_connected": state_data["sensor_connected"],
            "modbus_connected": state_data["modbus_connected"],
            "mqtt_connected": self.connected,
            "timestamp_ms": state_data["timestamp_ms"],
        }
        relay_state = getattr(self.relay, "last_known_state", {})
        relay_status = getattr(self.relay, "communication_status", None)
        relay_connected = (
            relay_status if state_data["modbus_connected"] and type(relay_status) is bool
            else False if not state_data["modbus_connected"] else None
        )
        values.update(
            {
                "relay_connected": relay_connected,
                "relay_feedback_heater": self._relay_feedback(
                    relay_state, "heater", relay_connected
                ),
                "relay_feedback_process_fan": self._relay_feedback(
                    relay_state, "process_fan", relay_connected
                ),
                "active_alarm": json.dumps(self._alarm_data(alarm), ensure_ascii=False),
            }
        )
        with self._alarm_lock:
            events = list(self._pending_alarm_events)
        if events:
            values["alarm_event"] = json.dumps(events, ensure_ascii=False)
        packet = {
            "ts": time.time_ns() // 1_000_000,
            "values": values,
        }
        if not self.connected:
            self._warn_unavailable()
            return
        try:
            result = self._client.publish(
                "v1/devices/me/telemetry",
                json.dumps(packet, ensure_ascii=False, allow_nan=False),
                qos=1,
            )
        except (TypeError, ValueError, OSError) as error:
            logger.warning("Could not publish ThingsBoard telemetry: %s", error)
            return
        if result.rc != self._mqtt.MQTT_ERR_SUCCESS:
            self._warn_unavailable()
        else:
            with self._alarm_lock:
                del self._pending_alarm_events[: len(events)]

    def observe_alarm(self) -> None:
        """Ghi nhận thời điểm cảnh báo bật/tắt để gửi thành lịch sử lên cloud."""
        alarm = self.controller.get_active_alarm()
        with self._alarm_lock:
            self._pending_alarm_events.extend(self._alarm_events(alarm))

    def _telemetry_loop(self) -> None:
        """Gửi telemetry định kỳ và giữ vòng điều khiển sống khi cloud lỗi."""
        while not self._telemetry_stop.wait(1.0):
            try:
                self.publish_telemetry()
            except Exception:
                logger.exception(
                    "Cloud telemetry publisher failed; local control continues."
                )

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        """Cập nhật kết nối và đăng ký nhận RPC của thiết bị từ ThingsBoard."""
        if getattr(reason_code, "is_failure", False):
            logger.warning("ThingsBoard MQTT connection rejected: %s", reason_code)
            with self._connection_lock:
                self._connected = False
            return
        with self._connection_lock:
            self._connected = True
        logger.info("Connected to ThingsBoard MQTT.")
        client.subscribe("v1/devices/me/rpc/request/+")

    def _on_disconnect(self, _client, _userdata, _disconnect_flags, reason_code, _properties) -> None:
        """Đánh dấu mất kết nối và ghi log nếu broker đóng kết nối bất thường."""
        with self._connection_lock:
            self._connected = False
        if getattr(reason_code, "is_failure", False):
            logger.warning("ThingsBoard MQTT disconnected: %s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:
        """Kiểm tra payload RPC rồi xếp yêu cầu để runtime xử lý an toàn."""
        # Không điều khiển relay tại callback; controller sẽ xác nhận trong vòng runtime.
        request_id = message.topic.rsplit("/", 1)[-1]
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._publish_rpc_response(
                request_id,
                {"accepted": False, "message": "RPC payload must be valid JSON."},
            )
            return
        if not isinstance(payload, Mapping):
            self._publish_rpc_response(
                request_id,
                {"accepted": False, "message": "RPC payload must be an object."},
            )
            return
        try:
            command_type = CommandType(payload.get("method"))
        except (TypeError, ValueError):
            self._publish_rpc_response(
                request_id,
                {"accepted": False, "message": "Remote command is not allowed."},
            )
            return
        if command_type not in REMOTE_COMMANDS:
            self._publish_rpc_response(
                request_id,
                {"accepted": False, "message": "Remote command is not allowed."},
            )
            return
        params = payload.get("params", {})
        if not isinstance(params, Mapping):
            self._publish_rpc_response(
                request_id,
                {"accepted": False, "message": "RPC params must be an object."},
            )
            return
        timestamp_ms = params.get("timestamp_ms", time.time_ns() // 1_000_000)
        if type(timestamp_ms) is not int:
            self._publish_rpc_response(
                request_id,
                {"accepted": False, "message": "timestamp_ms must be an integer."},
            )
            return
        supplied_id = params.get("command_id")
        if supplied_id is not None and (
            not isinstance(supplied_id, str) or not 0 < len(supplied_id) <= 128
        ):
            self._publish_rpc_response(
                request_id,
                {"accepted": False, "message": "command_id is invalid."},
            )
            return
        command_id = supplied_id if supplied_id is not None else str(uuid4())
        raw_value = params.get("value")
        if command_type == CommandType.SET_RECIPE and type(raw_value) is dict:
            # Controller xác thực toàn bộ bộ thông số và áp dụng nguyên tử dưới khóa.
            value = raw_value
        elif raw_value is None or type(raw_value) in (bool, str, float):
            value = raw_value
        elif type(raw_value) is int:
            try:
                value = float(raw_value)
            except OverflowError:
                self._publish_rpc_response(
                    request_id,
                    {"command_id": command_id, "accepted": False,
                     "message": "Remote numeric value is out of range."},
                )
                return
        else:
            self._publish_rpc_response(
                request_id,
                {"command_id": command_id, "accepted": False,
                 "message": "Remote command value has an invalid type."},
            )
            return
        if type(value) is float and not math.isfinite(value):
            self._publish_rpc_response(
                request_id,
                {"command_id": command_id, "accepted": False,
                 "message": "Remote command value must be finite."},
            )
            return
        command = ControlCommand(
            command_type=command_type,
            source=CommandSource.REMOTE_SCADA,
            value=value,
            command_id=command_id,
            timestamp_ms=timestamp_ms,
        )
        try:
            self._pending.put_nowait((request_id, command))
        except queue.Full:
            self._publish_rpc_response(
                request_id,
                {
                    "command_id": command_id,
                    "accepted": False,
                    "message": "Remote command queue is full.",
                },
            )

    def _publish_rpc_response(self, request_id: str, payload: dict[str, object]) -> None:
        """Đăng phản hồi chấp nhận/từ chối lên topic RPC response của thiết bị."""
        topic = f"v1/devices/me/rpc/response/{request_id}"
        try:
            result = self._client.publish(
                topic, json.dumps(payload, ensure_ascii=False), qos=1
            )
        except (TypeError, ValueError, OSError) as error:
            logger.warning("Could not publish RPC response %s: %s", request_id, error)
            return
        if result.rc != self._mqtt.MQTT_ERR_SUCCESS:
            logger.warning("RPC response %s was not accepted by the MQTT client.", request_id)

    def _alarm_events(self, alarm) -> list[dict[str, object]]:
        """Tạo sự kiện khi cảnh báo mới xuất hiện hoặc cảnh báo hiện tại được xóa."""
        events: list[dict[str, object]] = []
        if alarm is not None:
            alarm_data = self._alarm_data(alarm)
            if alarm_data is None:
                return events
            alarm_id = alarm_data.get("alarm_id")
            if isinstance(alarm_id, str) and alarm_id != self._last_alarm_id:
                if self._last_alarm_id and self._last_alarm:
                    events.append(
                        {
                            **self._last_alarm,
                            "active": False,
                            "cleared_at_ms": alarm_data.get("raised_at_ms"),
                        }
                    )
                self._last_alarm_id = alarm_id
                self._last_alarm = alarm_data
                events.append({**alarm_data, "active": True})
                return events
            self._last_alarm = alarm_data
            return events
        if self._last_alarm_id and self._last_alarm:
            events.append({
                **self._last_alarm,
                "active": False,
                "cleared_at_ms": time.time_ns() // 1_000_000,
            })
            self._last_alarm_id = None
            self._last_alarm = None
        return events

    @staticmethod
    def _alarm_data(alarm) -> dict[str, object] | None:
        """Chuyển đối tượng cảnh báo thành dữ liệu JSON an toàn cho telemetry."""
        if alarm is None:
            return None
        data = asdict(alarm)
        return {
            **data,
            "code": ThingsBoardMqttClient._enum_value(data.get("code")),
            "severity": ThingsBoardMqttClient._enum_value(data.get("severity")),
        }

    @staticmethod
    def _enum_value(value: object) -> object:
        """Chuyển enum thành chuỗi giá trị có thể gửi qua JSON."""
        return getattr(value, "value", value)

    @staticmethod
    def _relay_feedback(relay_state, name: str, relay_connected: object) -> object:
        """Lấy phản hồi relay nếu kết nối còn hợp lệ, nếu không trả về chưa rõ."""
        if (
            isinstance(relay_state, Mapping)
            and relay_connected is True
            and type(relay_state.get(name)) is bool
        ):
            return relay_state[name]
        return None

    def _warn_unavailable(self) -> None:
        """Giới hạn tần suất cảnh báo log khi broker hoặc publish chưa sẵn sàng."""
        now = time.monotonic()
        if now - self._last_publish_warning >= 30:
            logger.warning("ThingsBoard MQTT is unavailable; local control continues.")
            self._last_publish_warning = now
