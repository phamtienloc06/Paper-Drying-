"""Backend Flask xác thực người vận hành và nối giao diện với ThingsBoard."""

from __future__ import annotations

from functools import wraps
import hmac
import logging
import math
import os
from pathlib import Path
import secrets
import time
from typing import Callable
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from .thingsboard_client import MAX_HISTORY_BUCKETS, ThingsBoardClient, ThingsBoardError

logger = logging.getLogger(__name__)
# Whitelist lệnh từ xa; controller máy 1 vẫn kiểm tra quyền và an toàn lần cuối.
ALLOWED_COMMANDS = {
    "SET_MODE",
    "START_DRYING",
    "SET_TEMPERATURE",
    "SET_DRYING_DURATION",
    "SET_COOLING_TEMPERATURE",
    "SET_RECIPE",
    "EMERGENCY_STOP",
}
# Nhóm lệnh cập nhật thông số mẻ, có kiểm tra số riêng trước khi gửi RPC.
SETTING_COMMANDS = {
    "SET_TEMPERATURE",
    "SET_DRYING_DURATION",
    "SET_COOLING_TEMPERATURE",
}
# Các khoảng lịch sử giao diện được phép yêu cầu từ ThingsBoard.
HISTORY_RANGES_HOURS = {1, 6, 24, 168}


def create_app(
    thingsboard: ThingsBoardClient,
    *,
    scada_username: str,
    scada_password: str,
    secret_key: str,
    cookie_secure: bool = False,
) -> Flask:
    """Tạo ứng dụng Flask, các trang đăng nhập/dashboard và những API SCADA."""
    # Tài khoản web SCADA tách biệt với tài khoản ThingsBoard mà backend sử dụng.
    if not all((scada_username, scada_password, secret_key)):
        raise ValueError("SCADA username, password and secret key are required.")
    if len(secret_key) < 32 or secret_key.startswith("replace_with"):
        raise ValueError("SCADA_SECRET_KEY must be a non-placeholder secret of at least 32 characters.")
    app = Flask(
        __name__,
        template_folder="frontend",
        static_folder="frontend",
        static_url_path="/static",
    )
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=cookie_secure,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
        JSON_SORT_KEYS=False,
    )

    @app.after_request
    def disable_cache(response):
        """Thêm tiêu đề bảo mật và ngăn lưu nội dung nhạy cảm trong cache."""
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    def authenticated(view: Callable):
        """Tạo bộ trang trí chỉ cho phép phiên đã đăng nhập truy cập endpoint."""
        @wraps(view)
        def wrapped(*args, **kwargs):
            """Từ chối yêu cầu chưa xác thực bằng trang đăng nhập hoặc lỗi API."""
            if not session.get("authenticated"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Đăng nhập để tiếp tục."}), 401
                return render_template("login.html", csrf_token=_csrf_token()), 401
            return view(*args, **kwargs)

        return wrapped

    def require_csrf():
        """So sánh CSRF token gửi kèm với token lưu trong phiên hiện tại."""
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    @app.get("/")
    def index():
        """Hiển thị trang đăng nhập hoặc dashboard tùy theo phiên người dùng."""
        if not session.get("authenticated"):
            return render_template("login.html", csrf_token=_csrf_token())
        return render_template("index.html", csrf_token=_csrf_token())

    @app.post("/login")
    def login():
        """Kiểm tra thông tin tài khoản SCADA và tạo phiên đăng nhập mới."""
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not (expected and supplied and hmac.compare_digest(expected, supplied)):
            return render_template(
                "login.html",
                csrf_token=_csrf_token(),
                error="Phiên đăng nhập không hợp lệ. Hãy tải lại trang.",
            ), 400
        payload = request.form
        user_ok = hmac.compare_digest(payload.get("username", ""), scada_username)
        password_ok = hmac.compare_digest(payload.get("password", ""), scada_password)
        if not (user_ok and password_ok):
            logger.warning("Rejected SCADA login attempt.")
            return render_template(
                "login.html",
                csrf_token=_csrf_token(),
                error="Tên đăng nhập hoặc mật khẩu không đúng.",
            ), 401
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("index"))

    @app.post("/logout")
    @authenticated
    def logout():
        """Xóa phiên người dùng sau khi xác thực CSRF."""
        if not require_csrf():
            return jsonify({"error": "CSRF token không hợp lệ."}), 400
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/session")
    @authenticated
    def api_session():
        """Trả trạng thái phiên và CSRF token cho giao diện đã đăng nhập."""
        return jsonify({"authenticated": True, "csrf_token": _csrf_token()})

    @app.get("/api/state")
    @authenticated
    def api_state():
        """Trả telemetry gần nhất do backend lấy từ cache hoặc REST ThingsBoard."""
        # Trình duyệt chỉ gọi backend này; thông tin đăng nhập ThingsBoard ở lại máy chủ.
        try:
            state = thingsboard.latest_state()
        except ThingsBoardError as error:
            logger.warning("Could not load current telemetry: %s", error)
            return jsonify({"error": str(error)}), 502
        now_ms = time.time_ns() // 1_000_000
        timestamp_ms = state.get("timestamp_ms")
        telemetry_age_s = (
            max(0.0, (now_ms - timestamp_ms) / 1000)
            if type(timestamp_ms) is int
            else None
        )
        return jsonify(
            {
                "state": state,
                "server_time_ms": now_ms,
                "telemetry_age_s": telemetry_age_s,
            }
        )

    @app.get("/api/history")
    @authenticated
    def api_history():
        """Đọc chuỗi nhiệt độ và sự kiện cảnh báo trong khoảng thời gian cho phép."""
        try:
            hours = int(request.args.get("hours", "24"))
        except ValueError:
            return jsonify({"error": "Khoảng lịch sử không hợp lệ."}), 400
        if hours not in HISTORY_RANGES_HOURS:
            return jsonify({"error": "Khoảng lịch sử phải là 1, 6, 24 hoặc 168 giờ."}), 400
        end_ts = time.time_ns() // 1_000_000
        start_ts = end_ts - hours * 60 * 60 * 1000
        try:
            series = thingsboard.telemetry_history(start_ts, end_ts)
        except ThingsBoardError as error:
            logger.warning("Could not load telemetry history: %s", error)
            return jsonify({"error": str(error)}), 502
        alarm_events = []
        for point in series.get("alarm_event", []):
            event = point.get("value")
            if isinstance(event, dict):
                alarm_events.append(event)
            elif isinstance(event, list):
                alarm_events.extend(item for item in event if isinstance(item, dict))
        alarm_events.sort(key=_alarm_event_timestamp)
        return jsonify(
            {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "hours": hours,
                "series": series,
                "alarm_events": alarm_events,
                "aggregation_interval_ms": max(1000, (end_ts - start_ts + MAX_HISTORY_BUCKETS - 1) // MAX_HISTORY_BUCKETS),
            }
        )

    @app.post("/api/commands")
    @authenticated
    def api_command():
        """Kiểm tra yêu cầu từ trình duyệt rồi chờ phản hồi RPC từ controller máy 1."""
        # Kiểm tra đầu vào tại backend trước khi gửi RPC và chờ kết quả từ máy 1.
        if not require_csrf():
            return jsonify({"error": "CSRF token không hợp lệ."}), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Body phải là JSON object."}), 400
        command_type = payload.get("command_type")
        if not isinstance(command_type, str) or command_type not in ALLOWED_COMMANDS:
            return jsonify({"error": "Lệnh SCADA không được phép."}), 400
        value = payload.get("value")
        validation_error = _validate_command(command_type, value, "value" in payload)
        if validation_error:
            return jsonify({"error": validation_error}), 400

        command_id = str(uuid4())
        params = {
            "command_id": command_id,
            "timestamp_ms": time.time_ns() // 1_000_000,
        }
        if command_type != "EMERGENCY_STOP":
            params["value"] = value
        try:
            result = thingsboard.send_rpc(command_type, params)
        except ThingsBoardError as error:
            logger.warning("SCADA command %s failed: %s", command_id, error)
            return jsonify(
                {
                    "command_id": command_id,
                    "accepted": False,
                    "message": f"Không gửi/nhận được phản hồi từ controller: {error}",
                }
            ), 502
        if not isinstance(result, dict) or type(result.get("accepted")) is not bool:
            logger.error("Controller returned an invalid response for %s.", command_id)
            return jsonify(
                {
                    "command_id": command_id,
                    "accepted": False,
                    "message": "Controller trả về kết quả không hợp lệ.",
                }
            ), 502
        response = {
            "command_id": result.get("command_id", command_id),
            "accepted": result["accepted"],
            "message": result.get("message", "Không có thông điệp từ controller."),
        }
        return jsonify(response), 200 if result["accepted"] else 409

    app.extensions["thingsboard_client"] = thingsboard
    return app


def _csrf_token() -> str:
    """Lấy CSRF token của phiên hoặc tạo token mới nếu chưa có."""
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _alarm_event_timestamp(event: dict[str, object]) -> int:
    """Chọn thời điểm kích hoạt hoặc xóa cảnh báo để sắp xếp lịch sử."""
    timestamp = (
        event.get("cleared_at_ms")
        if event.get("active") is False
        else event.get("raised_at_ms")
    )
    return timestamp if type(timestamp) is int else 0


def _validate_command(
    command_type: str, value: object, value_was_sent: bool
) -> str | None:
    """Kiểm tra loại lệnh và kiểu/giá trị trước khi chuyển tiếp tới ThingsBoard."""
    if command_type == "SET_MODE":
        if not isinstance(value, str) or value not in {"AUTO", "OFF"}:
            return "SCADA chỉ được chọn chế độ AUTO hoặc OFF."
    elif command_type == "SET_RECIPE":
        fields = {"temperature_setpoint_c", "drying_duration_s", "cooling_stop_temperature_c"}
        if type(value) is not dict or set(value) != fields:
            return "Bộ thông số phải gồm đúng nhiệt độ đặt, thời gian giữ nhiệt và ngưỡng dừng quạt."
        for key, number in value.items():
            method = "SET_DRYING_DURATION" if key == "drying_duration_s" else "SET_TEMPERATURE"
            error = _validate_command(method, number, True)
            if error:
                return error
        if value["cooling_stop_temperature_c"] < 0:
            return "Ngưỡng dừng quạt phải không âm."
    elif command_type in SETTING_COMMANDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "Giá trị phải là số hữu hạn."
        try:
            if not math.isfinite(value):
                return "Giá trị phải là số hữu hạn."
        except OverflowError:
            return "Giá trị phải là số hữu hạn."
        if command_type == "SET_DRYING_DURATION" and value <= 0:
            return "Thời gian sấy phải lớn hơn 0 giây."
    elif command_type == "START_DRYING":
        if value_was_sent and value is not None:
            return "START_DRYING không nhận value."
    return None


def main() -> int:
    """Nạp remote_scada/.env, khởi tạo ThingsBoard và chạy máy chủ Flask."""
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise RuntimeError("Cài remote_scada/requirements.txt trước khi chạy SCADA.") from error
    load_dotenv(Path(__file__).with_name(".env"))
    required = ("TB_BASE_URL", "TB_USERNAME", "TB_PASSWORD", "TB_DEVICE_ID",
                "SCADA_USERNAME", "SCADA_PASSWORD", "SCADA_SECRET_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Thiếu biến môi trường: {', '.join(missing)}")
    placeholders = [name for name in required if os.environ[name].startswith("replace_with")]
    if placeholders:
        raise RuntimeError(f"Hãy thay giá trị mẫu trong biến: {', '.join(placeholders)}")
    try:
        rpc_timeout_s = float(os.getenv("TB_RPC_TIMEOUT_S", "15"))
        if not math.isfinite(rpc_timeout_s) or rpc_timeout_s <= 0:
            raise ValueError
    except ValueError as error:
        raise RuntimeError("TB_RPC_TIMEOUT_S phải là số hữu hạn dương.") from error
    thingsboard = ThingsBoardClient(
        os.environ["TB_BASE_URL"],
        os.environ["TB_USERNAME"],
        os.environ["TB_PASSWORD"],
        os.environ["TB_DEVICE_ID"],
        rpc_timeout_s=rpc_timeout_s,
    )
    host = os.getenv("SCADA_HOST", "127.0.0.1").strip()
    try:
        port = int(os.getenv("SCADA_PORT", "8080"))
    except ValueError as error:
        raise RuntimeError("SCADA_PORT must be an integer from 1 to 65535.") from error
    if not host or not 1 <= port <= 65535:
        raise RuntimeError("SCADA_HOST must not be empty and SCADA_PORT must be 1..65535.")
    secure_cookie = os.getenv("SCADA_COOKIE_SECURE", "false").lower()
    if secure_cookie not in {"true", "false"}:
        raise RuntimeError("SCADA_COOKIE_SECURE must be true or false.")
    cookie_secure = secure_cookie == "true"
    if not cookie_secure and host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            "Set SCADA_COOKIE_SECURE=true before binding SCADA to a network interface."
        )
    app = create_app(
        thingsboard,
        scada_username=os.environ["SCADA_USERNAME"],
        scada_password=os.environ["SCADA_PASSWORD"],
        secret_key=os.environ["SCADA_SECRET_KEY"],
        cookie_secure=cookie_secure,
    )
    thingsboard.start()
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        thingsboard.stop()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
