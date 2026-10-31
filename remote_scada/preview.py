"""Xem thử SCADA trên localhost bằng controller mô phỏng, không COM hoặc ThingsBoard.

    python -m remote_scada.preview
    http://127.0.0.1:8081 · operator / demo-scada

Chỉ dùng để phát triển giao diện. Lịch sử nền được tạo minh họa và gắn nhãn mô phỏng.
"""

import argparse
from collections import deque
from dataclasses import asdict
import math
import secrets
import threading
import time

from control_station.configuration import load_configuration
from control_station.hmi.app import _DemoPlant
from control_station.models import CommandSource, CommandType, ControlCommand
from .server import create_app


class PreviewGateway:
    def __init__(self, plant):
        self.plant = plant
        self._lock = threading.Lock()
        self._events = []
        self._alarm_id = None
        now = time.time_ns() // 1_000_000
        self._samples = deque(maxlen=7200)
        # Dữ liệu minh họa chỉ nằm trong launcher preview; production không dùng gateway này.
        for index in range(120):
            self._samples.append({
                "ts": now - (120 - index) * 15000,
                "temperature_c": round(28 + 7 * math.sin(index / 24) ** 2, 2),
                "temperature_setpoint_c": 60.0,
            })

    def latest_state(self):
        state = asdict(self.plant.controller.get_state_snapshot())
        state = {key: getattr(value, "value", value) for key, value in state.items()}
        state.update(asdict(self.plant.controller.get_settings_snapshot()))
        alarm = self.plant.controller.get_active_alarm()
        state.update(
            simulation=True, active_alarm=asdict(alarm) if alarm else None,
            relay_connected=self.plant.relay.communication_status,
            relay_feedback_heater=self.plant.relay.heater_on,
            relay_feedback_process_fan=self.plant.relay.process_fan_on,
        )
        now = time.time_ns() // 1_000_000
        with self._lock:
            if not self._samples or now - self._samples[-1]["ts"] >= 1000:
                self._samples.append({"ts": now, "temperature_c": state["temperature_c"],
                                      "temperature_setpoint_c": state["temperature_setpoint_c"]})
            alarm_id = alarm.alarm_id if alarm else None
            if alarm_id != self._alarm_id:
                if self._alarm_id:
                    previous = next((e for e in reversed(self._events) if e["alarm_id"] == self._alarm_id), None)
                    if previous:
                        self._events.append({**previous, "active": False, "cleared_at_ms": now})
                if alarm:
                    self._events.append(asdict(alarm))
                self._alarm_id = alarm_id
        return state

    def telemetry_history(self, start_ts, end_ts):
        self.latest_state()
        with self._lock:
            return {
                **{key: [{"ts": sample["ts"], "value": sample[key]} for sample in self._samples
                          if start_ts <= sample["ts"] <= end_ts]
                   for key in ("temperature_c", "temperature_setpoint_c")},
                "alarm_event": [{"ts": event.get("cleared_at_ms") or event["raised_at_ms"], "value": dict(event)}
                                for event in self._events
                                if start_ts <= (event.get("cleared_at_ms") or event["raised_at_ms"]) <= end_ts],
            }

    def send_rpc(self, method, params):
        result = self.plant.controller.process_command(ControlCommand(
            command_type=CommandType(method), source=CommandSource.REMOTE_SCADA,
            value=params.get("value"), command_id=params["command_id"],
            timestamp_ms=params["timestamp_ms"],
        ))
        self.latest_state()
        return asdict(result)


def create_preview_app():
    plant = _DemoPlant(load_configuration())
    plant.start()
    app = create_app(PreviewGateway(plant), scada_username="operator",
                     scada_password="demo-scada", secret_key=secrets.token_urlsafe(32))
    app.config["SCADA_PREVIEW"] = True
    app.extensions["preview_plant"] = plant
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Cổng phải thuộc 1..65535.")
    app = create_preview_app()
    print("SCADA MÔ PHỎNG CỤC BỘ — không dùng COM/ThingsBoard")
    print("Đăng nhập: operator / demo-scada. Lịch sử nền là dữ liệu minh họa.")
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    finally:
        app.extensions["preview_plant"].stop()


if __name__ == "__main__":
    main()
