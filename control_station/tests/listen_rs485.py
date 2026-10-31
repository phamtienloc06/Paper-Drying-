"""Nghe thụ động RS485; không gửi yêu cầu, không bật/tắt hay cấu hình thiết bị.

Chạy độc lập, sau khi đóng HMI và mọi script đang dùng COM:
    python -X utf8 -m control_station.tests.listen_rs485 --seconds 20

Script chẩn đoán này mở serial trực tiếp để nghe, không khởi tạo controller.
Mỗi dòng RX là một đoạn byte nhận được, chưa chắc là một frame Modbus đầy đủ.
"""

import argparse
import math
import sys
import time

import serial

from control_station.configuration import DEFAULT_CONFIG_PATH, load_configuration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--port", help="Thay cổng COM trong YAML.")
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds phải là số hữu hạn lớn hơn 0.")

    try:
        config = load_configuration(args.config).modbus
        port = args.port or config["port"]
        print(
            f"Nghe {port} | {config['baudrate']} baud | "
            f"{config['bytesize']}{config['parity']}{config['stopbits']} | "
            f"{args.seconds:g} giây",
            flush=True,
        )
        print("TX: 0 byte. Chỉ nhận dữ liệu; Ctrl+C để dừng.", flush=True)
        total = 0
        with serial.Serial(
            port=port,
            baudrate=config["baudrate"],
            bytesize=config["bytesize"],
            parity=config["parity"],
            stopbits=config["stopbits"],
            timeout=0.2,
        ) as connection:
            started = time.monotonic()
            while time.monotonic() - started < args.seconds:
                # Giới hạn kích thước để không đọc quá lâu khi bus có dữ liệu liên tục.
                data = connection.read(min(connection.in_waiting, 256) or 1)
                if data:
                    total += len(data)
                    print(
                        f"RX +{time.monotonic() - started:.3f}s: "
                        f"{data.hex(' ').upper()}",
                        flush=True,
                    )

        print(f"Kết thúc: TX=0 byte, RX={total} byte.")
        if total:
            print("Có dữ liệu dù script không gửi yêu cầu.")
            print("Nếu khung ID 2 lặp lại trong thời gian nghe, kiểm tra chế độ tự gửi PT100.")
        else:
            print("Chưa thấy dữ liệu tự phát trong khoảng nghe; chưa loại trừ chu kỳ dài hơn.")
        return 0
    except KeyboardInterrupt:
        print("\nĐã dừng nghe bus.")
        return 130
    except (OSError, ValueError) as error:
        print(f"Không nghe được bus: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
