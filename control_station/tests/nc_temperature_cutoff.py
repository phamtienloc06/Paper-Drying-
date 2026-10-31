"""Test riêng hai tải COM-NC: dưới ngưỡng thì chạy, đạt ngưỡng thì ngắt và kết thúc.

Script này kiểm tra ngắt tải, không thay thế chu trình AUTO. Khi module relay mất
nguồn, NC đóng trở lại; phần mềm không thể bảo đảm tải OFF trong tình huống đó.
"""

import argparse
from copy import deepcopy
import math
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_station.configuration import DEFAULT_CONFIG_PATH, load_configuration
from control_station.modbus import ModbusRTU
from control_station.pt100 import PT100Sensor
from control_station.relay import RelayModule


def set_loads(relay, powered):
    errors = []
    for name in ("heater", "process_fan"):
        try:
            relay.set_output(name, powered)
        except Exception as error:
            errors.append(f"{name}: {error}")
    if errors:
        raise RuntimeError("; ".join(errors))


def run_test(sensor, relay, threshold_c, max_wait_seconds, poll_interval_seconds):
    set_loads(relay, False)
    started, powered = time.monotonic(), False
    while True:
        temperature = sensor.read_temperature()
        print(
            f"Nhiệt độ: {temperature:.1f} °C / ngưỡng {threshold_c:.1f} °C", flush=True
        )
        if temperature >= threshold_c:
            set_loads(relay, False)
            return
        if time.monotonic() - started >= max_wait_seconds:
            raise TimeoutError("Hết thời gian chờ đạt ngưỡng.")
        if not powered:
            set_loads(relay, True)
            powered = True
        time.sleep(poll_interval_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--threshold-c", type=float)
    parser.add_argument("--max-wait-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    config = load_configuration(args.config)
    threshold = (
        config.control.temperature_setpoint_c
        if args.threshold_c is None
        else args.threshold_c
    )
    if (
        not math.isfinite(threshold)
        or not config.pt100["minimum_temperature_c"]
        <= threshold
        <= config.pt100["maximum_temperature_c"]
    ):
        parser.error("Ngưỡng nằm ngoài dải đo PT100.")
    if not math.isfinite(args.max_wait_seconds) or args.max_wait_seconds <= 0:
        parser.error("Thời gian chờ phải hữu hạn và lớn hơn 0.")
    relay_config = deepcopy(config.relay)
    for output in relay_config["outputs"].values():
        output["contact"] = "NC"
    client = ModbusRTU(config.modbus)
    sensor, relay = PT100Sensor(client, config.pt100), RelayModule(client, relay_config)
    connected, result = False, 0
    try:
        client.connect()
        connected = True
        run_test(
            sensor, relay, threshold, args.max_wait_seconds, config.poll_interval_s
        )
    except KeyboardInterrupt:
        result = 130
    except Exception as error:
        print(f"Test NC thất bại: {error}", file=sys.stderr)
        result = 1
    finally:
        if connected:
            try:
                relay.all_outputs_off()
            except Exception as error:
                print(
                    f"KHÔNG xác nhận tải OFF: {error}. Hãy ngắt nguồn tải.",
                    file=sys.stderr,
                )
                result = 1
        client.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
