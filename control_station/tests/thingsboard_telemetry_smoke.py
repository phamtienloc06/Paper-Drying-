"""Publish synthetic controller telemetry to ThingsBoard for a connection test."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from dotenv import load_dotenv

INTERVAL_S = 2.0
TELEMETRY_TOPIC = "v1/devices/me/telemetry"


def load_settings() -> tuple[str, int, str]:
    """Load the machine-1 MQTT settings without displaying the device token."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)

    token = os.getenv("TB_DEVICE_TOKEN", "").strip()
    host = os.getenv("TB_MQTT_HOST", "").strip()
    raw_port = os.getenv("TB_MQTT_PORT", "").strip()
    if not token or token.startswith("replace_with"):
        raise ValueError("Set a valid TB_DEVICE_TOKEN in control_station\\.env.")
    if not host:
        raise ValueError("Set TB_MQTT_HOST in control_station\\.env.")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError("Set TB_MQTT_PORT to 1883 or 8883.") from error
    if port not in {1883, 8883}:
        raise ValueError("This test supports MQTT port 1883 or TLS port 8883.")
    return host, port, token


def make_telemetry(cycle: int, now_ms: int) -> dict[str, object]:
    """Build a safe, synthetic OFF-state packet using the controller telemetry keys."""
    temperature = 25.0 + (cycle % 8) * 0.5
    values: dict[str, object] = {
        "temperature_c": temperature,
        "temperature_setpoint_c": 60.0,
        "heater_on": False,
        "process_fan_on": False,
        "auto_phase": "IDLE",
        "drying_elapsed_s": 0.0,
        "drying_remaining_s": 600.0,
        "hold_timer_running": False,
        "drying_duration_s": 600.0,
        "cooling_stop_temperature_c": 35.0,
        "mode": "OFF",
        "alarm_code": "NONE",
        "sensor_connected": True,
        "modbus_connected": True,
        "mqtt_connected": True,
        "timestamp_ms": now_ms,
        "relay_connected": True,
        "relay_feedback_heater": False,
        "relay_feedback_process_fan": False,
        "active_alarm": json.dumps(None),
    }
    return {"ts": now_ms, "values": values}


def main() -> int:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as error:
        raise RuntimeError(
            "Install control_station requirements, including paho-mqtt."
        ) from error

    host, port, token = load_settings()
    print(
        "This will publish synthetic OFF-state values to the configured ThingsBoard "
        "device every 2 seconds."
    )
    print(
        "Do not run it while the real controller publishes to the same device: "
        "the fake values will temporarily replace its latest telemetry."
    )
    if port == 1883:
        print("Warning: port 1883 is unencrypted MQTT; the device token is not protected in transit.")
    confirmation = input("Type SEND to continue: ").strip()
    if confirmation != "SEND":
        print("Cancelled; no MQTT connection was opened.")
        return 0

    connected = threading.Event()
    connection_error: list[str] = []
    disconnect_reason: list[str] = []

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"telemetry-smoke-{uuid4()}",
    )
    client.username_pw_set(token)
    client.enable_logger(logging.getLogger("paho.mqtt.client"))
    if port == 8883:
        client.tls_set()
        client.tls_insecure_set(False)

    def on_connect(_client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            connection_error.append(str(reason_code))
        connected.set()

    def on_disconnect(
        _client, _userdata, _disconnect_flags, reason_code, _properties
    ) -> None:
        disconnect_reason.append(str(reason_code))

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    logging.basicConfig(level=logging.INFO)
    print(f"Connecting to {host}:{port}...")
    try:
        client.connect(host, port, keepalive=60)
        client.loop_start()
        if not connected.wait(timeout=10):
            detail = (
                f" Last MQTT disconnect reason: {disconnect_reason[-1]}."
                if disconnect_reason
                else " No MQTT CONNACK was received."
            )
            raise TimeoutError(
                "Timed out waiting for the MQTT connection response."
                f"{detail} Check that the host, port, TLS setting, and network "
                "firewall match the ThingsBoard connectivity instructions."
            )
        if connection_error:
            raise ConnectionError(
                f"ThingsBoard rejected the MQTT connection: {connection_error[0]}"
            )

        print("Connected. Publishing synthetic telemetry; press Ctrl+C to stop.")
        cycle = 0
        next_publish = time.monotonic()
        while True:
            now_ms = time.time_ns() // 1_000_000
            packet = make_telemetry(cycle, now_ms)
            info = client.publish(
                TELEMETRY_TOPIC,
                json.dumps(packet, allow_nan=False),
                qos=1,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError(f"MQTT publish failed with code {info.rc}.")
            info.wait_for_publish(timeout=10)
            if not info.is_published():
                raise TimeoutError("Timed out waiting for ThingsBoard to accept telemetry.")
            print(
                f"Published sample {cycle + 1}: "
                f"temperature_c={packet['values']['temperature_c']} °C"
            )
            cycle += 1
            next_publish += INTERVAL_S
            delay = next_publish - time.monotonic()
            if delay <= 0:
                next_publish = time.monotonic()
            else:
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        client.disconnect()
        client.loop_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
