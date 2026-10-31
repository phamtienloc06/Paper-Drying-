"""Đọc PT100; --change-id là commissioning, --scan chỉ dùng khi cô lập cảm biến."""

import argparse
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_station.configuration import DEFAULT_CONFIG_PATH, load_configuration
from control_station.modbus import ModbusCommunicationError, ModbusRTU
from control_station.pt100 import PT100Sensor


def find_current_device_id(client, config):
    """Dò đọc thanh ghi ID; chỉ dùng khi bus đang có một module PT100."""
    candidates = list(dict.fromkeys([config["slave_id"]] + list(range(1, 248))))
    for slave_id in candidates:
        try:
            values = client.read_holding_registers(
                slave_id, config["device_id_register"], count=1
            )
            if len(values) == 1 and values[0] == slave_id:
                return slave_id
        except ModbusCommunicationError:
            continue
    return None


def format_elapsed(seconds):
    total = int(seconds)
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--scan", action="store_true", help="Dò ID; cô lập PT100 khỏi các module khác."
    )
    parser.add_argument("--change-id", action="store_true")
    parser.add_argument("--current-id", type=int)
    args = parser.parse_args(argv)
    if args.change_id and args.current_id is None and not args.scan:
        parser.error("--change-id cần --current-id hoặc --scan.")
    config = load_configuration(args.config)
    client = ModbusRTU(config.modbus)
    try:
        client.connect()
        active_id = config.pt100["slave_id"]
        if args.scan:
            active_id = find_current_device_id(client, config.pt100)
            if active_id is None:
                raise RuntimeError("Không tìm thấy PT100.")
        sensor = PT100Sensor(client, config.pt100)
        if args.change_id:
            sensor.change_slave_id(
                args.current_id if args.current_id is not None else active_id
            )
            time.sleep(1)
            active_id = config.pt100["slave_id"]
        sensor = PT100Sensor(client, {**config.pt100, "slave_id": active_id})
        keyboard = None
        if os.name == "nt":
            import msvcrt

            keyboard = msvcrt
        test_mode, started_at, elapsed = False, None, None
        print("Windows: T bật/tắt test, S bắt đầu/dừng đồng hồ; Ctrl+C dừng đọc.")
        while True:
            if keyboard is not None:
                while keyboard.kbhit():
                    key = keyboard.getwch().lower()
                    if key == "t":
                        test_mode, started_at, elapsed = not test_mode, None, None
                    elif key == "s" and test_mode:
                        if started_at is None:
                            started_at, elapsed = time.monotonic(), None
                        else:
                            elapsed, started_at = time.monotonic() - started_at, None
            raw, temperature = sensor.read_sample()
            line = f"COM: {config.modbus['port']} | ID: {active_id} | Raw: {raw} | Nhiệt độ: {temperature:.1f} °C"
            if test_mode:
                seconds = (
                    time.monotonic() - started_at if started_at is not None else elapsed
                )
                line += " | Thời gian: " + (
                    format_elapsed(seconds) if seconds is not None else "chưa bắt đầu"
                )
            print(line, flush=True)
            time.sleep(config.poll_interval_s)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"Đọc PT100 thất bại: {error}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
