"""Flask HMI cục bộ; chỉ giao tiếp với controller qua public API."""

from __future__ import annotations

import atexit
from collections.abc import Mapping
from dataclasses import asdict
import logging
import threading
import time
from typing import Protocol

from flask import Flask, jsonify, render_template, request

from ..controller import SystemController
from ..configuration import StationConfiguration, load_configuration
from ..models import (
    CommandResult,
    CommandSource,
    CommandType,
    ControlCommand,
    ControlSettings,
    SystemState,
)

logger = logging.getLogger(__name__)


class ControllerPort(Protocol):
    """Phần public API mà HMI được phép sử dụng."""

    def get_state_snapshot(self) -> SystemState: ...
    def get_settings_snapshot(self) -> ControlSettings: ...
    def get_active_alarm(self): ...
    def process_command(self, command: ControlCommand) -> CommandResult: ...


def _enum_values(data: dict) -> dict:
    """Đổi Enum trong dataclass thành chuỗi JSON."""
    return {key: getattr(value, "value", value) for key, value in data.items()}


def _state_payload(controller: ControllerPort, simulation: bool, relay=None) -> dict:
    state = _enum_values(asdict(controller.get_state_snapshot()))
    relay_status = getattr(relay, "communication_status", None)
    state["relay_connected"] = (
        relay_status if state["modbus_connected"] and type(relay_status) is bool
        else False if not state["modbus_connected"] else None
    )
    settings = _enum_values(asdict(controller.get_settings_snapshot()))
    cached = getattr(relay, "last_known_state", {})
    feedback = {
        name: cached.get(name) if isinstance(cached, Mapping)
        and state["relay_connected"] is True and type(cached.get(name)) is bool else None
        for name in ("heater", "process_fan")
    }
    alarm = controller.get_active_alarm()
    return {
        "state": state,
        "settings": settings,
        "relay_feedback": feedback,
        "active_alarm": _enum_values(asdict(alarm)) if alarm is not None else None,
        "simulation": simulation,
        "server_time_ms": time.time_ns() // 1_000_000,
    }


def create_app(controller: ControllerPort, *, simulation: bool = False, relay=None) -> Flask:
    """Tạo HMI gắn với một controller đã được khởi tạo ở máy 1."""
    app = Flask(__name__)
    app.config.update(JSON_SORT_KEYS=False, HMI_SIMULATION=simulation)

    @app.after_request
    def disable_api_cache(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def api_state():
        return jsonify(_state_payload(controller, simulation, relay))

    @app.post("/api/commands")
    def api_command():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(
                {"accepted": False, "message": "Body phải là JSON object."}
            ), 400

        try:
            command_type = CommandType(payload.get("command_type"))
        except (TypeError, ValueError):
            return jsonify(
                {"accepted": False, "message": "command_type không hợp lệ."}
            ), 400

        command = ControlCommand(
            command_type=command_type,
            source=CommandSource.LOCAL_HMI,
            value=payload.get("value"),
        )
        result = controller.process_command(command)
        response = _enum_values(asdict(result))
        response["state"] = _state_payload(controller, simulation, relay)["state"]
        return jsonify(response), 200 if result.accepted else 409

    @app.get("/api/health")
    def api_health():
        return jsonify({"status": "ok", "simulation": simulation})

    return app


class _MemoryRelay:
    """Relay trong RAM để chạy giao diện khi chưa có phần cứng."""

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


class _DemoPlant:
    """Mô hình nhiệt đơn giản để kiểm tra HMI và controller trên máy tính."""

    def __init__(self, config: StationConfiguration) -> None:
        self.config = config
        self.ambient_c = config.simulation["ambient_c"]
        self.temperature_c = self.ambient_c
        self.relay = _MemoryRelay()
        self.controller = SystemController(
            SystemState(),
            config.control,
            self.relay,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.mqtt_client = None

    def attach_mqtt(self, mqtt_client) -> None:
        """Cho mô phỏng dùng cùng cầu telemetry/RPC với runtime phần cứng."""
        if self.mqtt_client is not None:
            raise RuntimeError("MQTT client is already attached.")
        try:
            mqtt_client.start()
        except Exception:
            logger.exception("Could not start simulation cloud bridge; local control continues.")
            return
        self.mqtt_client = mqtt_client

    def start(self) -> None:
        self.poll_once()
        self._thread = threading.Thread(
            target=self._run, name="demo-thermal-plant", daemon=True
        )
        self._thread.start()

    def stop(self) -> bool:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join()
        try:
            return self.controller.shutdown()
        finally:
            self.relay.communication_status = False
            if self.mqtt_client is not None:
                try:
                    self.mqtt_client.stop()
                except Exception:
                    logger.exception("Could not stop simulation cloud bridge cleanly.")
            self.controller.update_mqtt_connection(False)

    def _run(self) -> None:
        last = time.monotonic()
        wait_s = self.config.poll_interval_s
        while not self._stop_event.wait(wait_s):
            now = time.monotonic()
            dt = min(now - last, 1.0)
            last = now
            self.poll_once(dt)
            wait_s = max(0.0, self.config.poll_interval_s - (time.monotonic() - now))

    def poll_once(self, dt: float = 0.0) -> None:
        """Cập nhật nhiệt mô phỏng và xử lý lệnh từ xa trong vòng điều khiển."""
        snapshot = self.controller.get_state_snapshot()
        if snapshot.heater_on:
            rate_c_per_s = self.config.simulation["heater_rate_c_per_s"]
        elif snapshot.process_fan_on:
            rate_c_per_s = -self.config.simulation["fan_cooling_rate_c_per_s"]
        else:
            rate_c_per_s = (
                self.ambient_c - self.temperature_c
            ) * self.config.simulation["ambient_loss_per_s"]

        self.temperature_c = max(
            self.ambient_c - 1.0,
            self.temperature_c + rate_c_per_s * dt,
        )
        self.controller.update_measurement(round(self.temperature_c, 2), True, True)
        if self.mqtt_client is not None:
            try:
                self.controller.update_mqtt_connection(self.mqtt_client.connected)
                self.mqtt_client.process_pending_commands()
            except Exception:
                logger.exception("Simulation cloud command bridge failed; local control continues.")
        else:
            self.controller.update_mqtt_connection(False)
        self.controller.run_control_cycle()
        if self.mqtt_client is not None:
            try:
                self.mqtt_client.observe_alarm()
            except Exception:
                logger.exception("Could not capture simulation cloud alarm event.")


def create_demo_app(config: StationConfiguration | None = None) -> Flask:
    """Tạo HMI có mô phỏng nhiệt, dùng được ngay khi chưa có Modbus."""
    plant = _DemoPlant(config if config is not None else load_configuration())
    plant.start()
    atexit.register(plant.stop)
    app = create_app(plant.controller, simulation=True, relay=plant.relay)
    app.extensions["demo_plant"] = plant
    return app


if __name__ == "__main__":
    from ..main import main

    raise SystemExit(main())
