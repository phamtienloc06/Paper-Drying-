"""So sánh FC01 đọc 8 -> 2 -> 8 bit, chỉ đọc, không thay đổi relay.

Chạy từ thư mục gốc: python -m control_station.tests.compare_relay_reads
COM/ID mặc định lấy từ YAML; cố định 9600 baud, 8N1, timeout 1000 ms, không retry.
Đóng HMI/script khác đang sử dụng cùng cổng COM trước khi chạy.
"""

import argparse
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_station.configuration import DEFAULT_CONFIG_PATH, load_configuration
from control_station.modbus import ModbusCommunicationError, ModbusRTU


def compare_reads(bus, slave_id):
    results = []
    for index, count in enumerate((8, 2, 8), start=1):
        print(f"Lần {index}: ID {slave_id}, FC01, địa chỉ 0, số lượng {count} bit.", flush=True)
        try:
            bits = bus.read_coils(slave_id=slave_id, address=0, count=count)
        except ModbusCommunicationError as error:
            print(f"  THẤT BẠI: {error}", flush=True)
            results.append(False)
        else:
            print(f"  THÀNH CÔNG: bit thô {[int(bit) for bit in bits]}.", flush=True)
            results.append(True)
        if index < 3:
            time.sleep(0.2)

    if results == [True, False, True]:
        print("Cùng cấu hình: cả hai lần đọc 8 bit thành công, đọc 2 bit thất bại.")
        print("Kết quả ủng hộ việc dùng yêu cầu đọc 8 bit cho module này.")
    elif all(results):
        print("Cả đọc 2 và 8 bit đều thành công; chưa có bằng chứng module từ chối 2 bit.")
    else:
        print("Chưa tách biệt được nguyên nhân do số lượng bit; xem kết quả từng lần đọc.")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--port", help="COM cần kiểm tra, thay cho YAML.")
    parser.add_argument("--slave-id", type=int, help="ID relay đã đọc được, thay cho YAML.")
    args = parser.parse_args(argv)
    if args.slave_id is not None and not 1 <= args.slave_id <= 255:
        parser.error("ID phải nằm trong dải 1..255.")

    bus = None
    try:
        config = load_configuration(args.config)
        slave_id = args.slave_id if args.slave_id is not None else int(config.relay["slave_id"])
        bus = ModbusRTU({
            **config.modbus,
            "port": args.port if args.port is not None else config.modbus["port"],
            "baudrate": 9600,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "timeout_ms": 1000,
            "retries": 0,
            "retry_delay_ms": 0,
        })
        print(f"COM: {bus.port} | ID: {slave_id} | 9600, 8N1 | timeout: 1000 ms | retry: 0")
        print("Chỉ đọc FC01; không bật/tắt, không đổi ID, không sửa YAML.")
        bus.connect()
        results = compare_reads(bus, slave_id)
        return 0 if results[0] and results[2] else 1
    except KeyboardInterrupt:
        print("\nĐã dừng kiểm tra.")
        return 130
    except Exception as error:
        print(f"Không thực hiện được phép thử: {error}", file=sys.stderr)
        return 1
    finally:
        if bus is not None:
            bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
