"""Chạy HMI và vòng điều khiển; mặc định mô phỏng, --hardware để dùng thiết bị thật."""

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from .configuration import DEFAULT_CONFIG_PATH, load_configuration
from .hmi.app import create_app, create_demo_app
from .runtime import StationRuntime


def main(argv=None) -> int:
    """Nạp cấu hình, khởi chạy HMI cùng runtime mô phỏng hoặc phần cứng."""
    # Nạp thông tin MQTT tùy chọn, chỉ dùng cho cầu nối ThingsBoard của máy 1.
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--hardware", action="store_true", help="Dùng PT100/relay thật; khởi động OFF."
    )
    parser.add_argument(
        "--mqtt", action="store_true",
        help="Bật ThingsBoard cả khi mô phỏng; cần TB_DEVICE_TOKEN trong control_station/.env.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_configuration(args.config)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    device_token = os.getenv("TB_DEVICE_TOKEN")
    if args.mqtt and (not device_token or device_token.startswith("replace_with")):
        parser.error("--mqtt cần TB_DEVICE_TOKEN hợp lệ trong control_station/.env.")
    runtime = None
    try:
        if args.hardware:
            runtime = StationRuntime(config)
            app = create_app(runtime.controller, relay=runtime.relay)
            app.extensions["station_runtime"] = runtime
        else:
            app = create_demo_app(config)
            runtime = app.extensions["demo_plant"]
        if (args.hardware or args.mqtt) and device_token:
            try:
                mqtt_port = int(os.getenv("TB_MQTT_PORT", "8883"))
                if not 1 <= mqtt_port <= 65535:
                    raise ValueError("TB_MQTT_PORT must be between 1 and 65535.")
                from .mqtt_client import ThingsBoardMqttClient

                cloud_client = ThingsBoardMqttClient(
                    device_token,
                    runtime.controller,
                    runtime.relay,
                    host=os.getenv("TB_MQTT_HOST", "mqtt.thingsboard.cloud"),
                    port=mqtt_port,
                    simulation=not args.hardware,
                )
            except (RuntimeError, ValueError) as error:
                if args.mqtt:
                    parser.error(str(error))
                logging.error("Cloud telemetry/RPC disabled: %s", error)
            else:
                runtime.attach_mqtt(cloud_client)
        if args.hardware:
            runtime.start()
        app.run(
            host=config.hmi_host, port=config.hmi_port, debug=False, use_reloader=False
        )
    except KeyboardInterrupt:
        pass
    finally:
        if runtime is not None:
            runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
