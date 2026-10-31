"""Chẩn đoán và kiểm tra phần cứng relay EPCB.

Chạy tương tác từ thư mục temperature_control_system:
    python -m control_station.tests.relay_sequence

Chọn doc, docrelay1-2 hoặc battat tại dấu nhắc. Script test luôn thử tắt
các relay đã kiểm tra khi kết thúc.
"""

from pathlib import Path
import argparse
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_station.configuration import (
    load_configuration as load_station_configuration,
)
from control_station.modbus import ModbusRTU


def load_configuration() -> tuple[dict, dict]:
    """Đọc cấu hình bus và relay từ config.yaml."""
    config = load_station_configuration()
    return config.modbus, config.relay


def read_relay_states(client: ModbusRTU, relay: dict) -> list[bool]:
    """Đọc số coil trạng thái theo datasheet và đổi bit thành True=ON/False=OFF."""
    coils = [
        int(relay["channels"]["relay_1_coil"]),
        int(relay["channels"]["relay_2_coil"]),
    ]
    first_coil = min(coils)
    second_coil = max(coils)
    if second_coil != first_coil + 1:
        raise ValueError("Hai coil relay phải liên tiếp trong config.yaml.")
    count = int(relay["status_coil_count"])
    raw_states = client.read_coils(
        slave_id=int(relay["slave_id"]),
        address=first_coil,
        count=count,
    )
    status_readback = relay["status_readback"]
    on_value = status_readback["on"]
    off_value = status_readback["off"]
    if (
        type(on_value) is not bool
        or type(off_value) is not bool
        or on_value == off_value
    ):
        raise ValueError("status_readback.on/off phải là hai giá trị bool đối nhau.")
    if len(raw_states) < count or any(type(value) is not bool for value in raw_states):
        raise RuntimeError("Phản hồi trạng thái coil relay không hợp lệ.")
    return [raw_states[coil - first_coil] == on_value for coil in coils]


def read_relay_channel_state(client: ModbusRTU, relay: dict, channel: int) -> bool:
    """Đọc riêng một coil để chẩn đoán từng kênh độc lập."""
    if channel not in (1, 2):
        raise ValueError("Kênh relay phải là 1 hoặc 2.")
    first_coil = min(
        int(relay["channels"]["relay_1_coil"]),
        int(relay["channels"]["relay_2_coil"]),
    )
    address = int(relay["channels"][f"relay_{channel}_coil"])
    count = int(relay["status_coil_count"])
    raw_states = client.read_coils(
        slave_id=int(relay["slave_id"]), address=first_coil, count=count
    )
    status_readback = relay["status_readback"]
    on_value = status_readback["on"]
    off_value = status_readback["off"]
    if (
        type(on_value) is not bool
        or type(off_value) is not bool
        or on_value == off_value
    ):
        raise ValueError("status_readback.on/off phải là hai giá trị bool đối nhau.")
    offset = address - first_coil
    if len(raw_states) < count or any(type(value) is not bool for value in raw_states):
        raise RuntimeError(f"Phản hồi coil relay {channel} không hợp lệ.")
    if not 0 <= offset < count:
        raise ValueError(f"Địa chỉ coil relay {channel} nằm ngoài dải đọc.")
    return raw_states[offset] == on_value


def verify_coils(client: ModbusRTU, relay: dict, expected: list[bool]) -> None:
    """Đọc FC01 và báo lỗi nếu trạng thái thật không khớp trạng thái mong đợi."""
    actual = read_relay_states(client, relay)
    if actual != expected:
        raise RuntimeError(f"Trạng thái relay sai: mong đợi {expected}, nhận {actual}.")
    print(f"Trạng thái relay: {actual}")


def verify_relay_channel(
    client: ModbusRTU, relay: dict, channel: int, expected: bool
) -> None:
    actual = read_relay_channel_state(client, relay, channel)
    if actual != expected:
        raise RuntimeError(
            f"Relay {channel} sai trạng thái: mong đợi {expected}, nhận {actual}."
        )
    print(f"Trạng thái relay {channel}: {actual}")


def check_relay_address(client: ModbusRTU, relay: dict) -> list[bool]:
    """Probe slave ID bằng FC01 đọc trạng thái, không thay đổi đầu ra relay."""
    slave_id = int(relay["slave_id"])
    try:
        states = read_relay_states(client, relay)
    except Exception as error:
        raise RuntimeError(
            f"Không đọc được trạng thái relay tại slave ID {slave_id} bằng FC01; "
            "chưa gửi lệnh bật/tắt. Kiểm tra ID, COM và serial."
        ) from error
    print(f"Relay phản hồi tại slave ID {slave_id}; trạng thái hiện tại: {states}.")
    return states


def check_relay_channel_address(
    client: ModbusRTU, relay: dict, channel: int
) -> bool:
    slave_id = int(relay["slave_id"])
    try:
        state = read_relay_channel_state(client, relay, channel)
    except Exception as error:
        raise RuntimeError(
            f"Không đọc được coil relay {channel} tại slave ID {slave_id}; "
            "chưa gửi lệnh bật/tắt."
        ) from error
    print(
        f"Relay {channel} phản hồi tại slave ID {slave_id}; trạng thái coil: {state}."
    )
    return state


def scan_relay_id(modbus_config: dict, relay: dict) -> tuple[int, int]:
    """Đọc ID relay qua FC03 broadcast ở các baud được khai báo; không ghi thiết bị."""
    register = int(relay["device_id_register"])
    supported_baudrates = [int(rate) for rate in relay["supported_baudrates"]]

    for baudrate in supported_baudrates:
        probe_config = {
            **modbus_config,
            "baudrate": baudrate,
            "timeout_ms": min(int(modbus_config.get("timeout_ms", 1000)), 500),
            "retries": 0,
            "retry_delay_ms": 0,
        }
        client = ModbusRTU(probe_config)
        try:
            client.connect()
            values = client.read_holding_registers(
                slave_id=int(relay["device_id_read_broadcast_slave_id"]),
                address=register,
                count=1,
                allow_broadcast=True,
            )
            device_id = int(values[0])
            if not 1 <= device_id <= 255:
                raise ValueError(f"ID relay trả về ngoài dải 1..255: {device_id}.")
            print(f"Tìm thấy relay: slave ID {device_id}, baud {baudrate}.")
            return device_id, baudrate
        except Exception as error:
            print(f"Không đọc được ID relay ở baud {baudrate}: {error}")
        finally:
            client.close()

    raise RuntimeError(
        "Không đọc được ID relay qua FC03 broadcast ở các baud đã cấu hình. "
        "Kiểm tra đúng model/datasheet, nguồn, dây RS485 và baud."
    )


def set_relay_id(modbus_config: dict, relay: dict, new_id: int) -> None:
    """Đổi ID relay bằng FC16 broadcast rồi xác nhận lại bằng FC03 broadcast."""
    if type(new_id) is not int or not 1 <= new_id <= 255:
        raise ValueError("ID relay mới phải là số nguyên trong dải 1..255.")
    register = int(relay["device_id_register"])
    broadcast_id = int(relay["device_id_broadcast_slave_id"])
    read_broadcast_id = int(relay["device_id_read_broadcast_slave_id"])

    client = ModbusRTU(modbus_config)
    try:
        client.connect()
        current_id = client.read_holding_registers(
            slave_id=read_broadcast_id,
            address=register,
            count=1,
            allow_broadcast=True,
        )[0]
        print(f"ID relay hiện tại: {current_id}.")
        if current_id == new_id:
            print(f"Relay đã có ID {new_id}; không cần ghi lại.")
            return
        client.write_registers(
            slave_id=broadcast_id,
            address=register,
            values=[new_id],
            no_response_expected=True,
        )
    finally:
        client.close()

    time.sleep(0.5)
    if scan_relay_id(
        {**modbus_config, "baudrate": int(modbus_config["baudrate"])},
        {**relay, "supported_baudrates": [int(modbus_config["baudrate"])]},
    )[0] != new_id:
        raise RuntimeError(f"Không xác nhận được relay đã đổi sang ID {new_id}.")
    print(f"Đã xác nhận relay ID {new_id}.")


def scan_relay_baud(modbus_config: dict, relay: dict) -> tuple[int, int, int]:
    """Thử đọc mã baud tại các baud/ID đã biết; chỉ gửi FC03, không ghi thiết bị."""
    register = int(relay["baudrate_read_register"])
    supported_baudrates = [int(rate) for rate in relay["supported_baudrates"]]
    candidate_ids = list(
        dict.fromkeys((int(relay["slave_id"]), int(relay["default_slave_id"])))
    )
    baudrate_codes = {
        int(code): int(rate) for rate, code in relay["baudrate_codes"].items()
    }

    for baudrate in supported_baudrates:
        probe_config = {
            **modbus_config,
            "baudrate": baudrate,
            "timeout_ms": min(int(modbus_config.get("timeout_ms", 1000)), 500),
            "retries": 0,
            "retry_delay_ms": 0,
        }
        client = ModbusRTU(probe_config)
        try:
            client.connect()
            for slave_id in candidate_ids:
                try:
                    values = client.read_holding_registers(
                        slave_id=slave_id,
                        address=register,
                        count=1,
                    )
                except Exception:
                    continue

                code = int(values[0])
                if code not in baudrate_codes:
                    print(
                        f"Có phản hồi tại baud {baudrate}, ID {slave_id}, "
                        f"nhưng mã baud {code} không nằm trong bảng datasheet."
                    )
                    continue
                detected_baudrate = baudrate_codes[code]
                print(
                    f"Đọc được register 0x{register:04X}: code {code}, "
                    f"baud thiết bị {detected_baudrate}, tốc độ probe {baudrate}, "
                    f"slave ID {slave_id}."
                )
                return detected_baudrate, baudrate, slave_id
        except Exception as error:
            print(f"Không mở được COM ở baud {baudrate}: {error}")
        finally:
            client.close()

    raise RuntimeError(
        "Không đọc được register baud 0x03E8 ở các baud 4800/9600/19200 "
        "với slave ID mục tiêu hoặc mặc định. Chỉ các tổ hợp này đã được thử; "
        "chưa gửi lệnh ghi hay bật/tắt relay."
    )


def run_sequence(
    client: ModbusRTU,
    relay: dict,
    initial_states: list[bool],
) -> None:
    """Bật rồi tắt từng relay bằng FC05 theo frame điều khiển riêng từng kênh."""
    channels = relay["channels"]
    relay_1_coil = int(channels["relay_1_coil"])
    relay_2_coil = int(channels["relay_2_coil"])
    if relay_2_coil != relay_1_coil + 1:
        raise ValueError("Hai coil relay phải liên tiếp trong config.yaml.")

    slave_id = int(relay["slave_id"])
    # Đây là chẩn đoán cuộn relay, không phải chu trình điều khiển tải AUTO.
    client.write_coil(slave_id, relay_2_coil, False)
    client.write_coil(slave_id, relay_1_coil, False)
    initial_states = [False, False]
    print("Bật relay 1.")
    client.write_coil(slave_id, relay_1_coil, True)
    verify_coils(client, relay, [True, initial_states[1]])

    print("Giữ relay 1 ON trong 5 giây...")
    time.sleep(5)

    print("Tắt relay 1.")
    client.write_coil(slave_id, relay_1_coil, False)
    verify_coils(client, relay, [False, initial_states[1]])

    print("Bật relay 2.")
    client.write_coil(slave_id, relay_2_coil, True)
    verify_coils(client, relay, [False, True])

    print("Giữ relay 2 ON trong 2 giây...")
    time.sleep(2)

    print("Tắt relay 2.")
    client.write_coil(slave_id, relay_2_coil, False)
    verify_coils(client, relay, [False, False])


def run_channel_sequence(client: ModbusRTU, relay: dict, channel: int) -> None:
    """Bật rồi tắt riêng một relay, kiểm tra trạng thái sau từng lệnh."""
    coil = int(relay["channels"][f"relay_{channel}_coil"])
    slave_id = int(relay["slave_id"])
    hold_seconds = 5 if channel == 1 else 2

    client.write_coil(slave_id, coil, False)
    verify_relay_channel(client, relay, channel, False)
    print(f"Bật relay {channel}.")
    client.write_coil(slave_id, coil, True)
    verify_relay_channel(client, relay, channel, True)
    print(f"Giữ relay {channel} ON trong {hold_seconds} giây...")
    time.sleep(hold_seconds)
    print(f"Tắt relay {channel}.")
    client.write_coil(slave_id, coil, False)
    verify_relay_channel(client, relay, channel, False)


def run_hardware_test(
    modbus_config: dict,
    relay_config: dict,
    *,
    channel: int | None = None,
    check_only: bool = False,
) -> int:
    client = ModbusRTU(modbus_config)
    connected = False
    address_checked = False
    try:
        client.connect()
        connected = True
        if channel is None:
            initial_states = check_relay_address(client, relay_config)
        else:
            check_relay_channel_address(client, relay_config, channel)
            initial_states = []
        address_checked = True
        if check_only:
            print("Probe hoàn tất; không có lệnh bật/tắt nào được gửi.")
            return 0
        if channel is None:
            run_sequence(client, relay_config, initial_states)
        else:
            run_channel_sequence(client, relay_config, channel)
        print("Bài test hoàn tất.")
        return 0
    except KeyboardInterrupt:
        print("Đã dừng bài test relay.")
        return 130
    except Exception as error:
        print(f"Test relay thất bại: {error}", file=sys.stderr)
        return 1
    finally:
        if connected and address_checked and not check_only:
            try:
                relay_slave_id = int(relay_config["slave_id"])
                if channel is None:
                    for key in ("relay_1_coil", "relay_2_coil"):
                        client.write_coil(
                            relay_slave_id, int(relay_config["channels"][key]), False
                        )
                    print("Đã gửi lệnh tắt cả hai relay.")
                else:
                    coil = int(relay_config["channels"][f"relay_{channel}_coil"])
                    client.write_coil(relay_slave_id, coil, False)
                    print(f"Đã gửi lệnh tắt relay {channel}.")
            except Exception as error:
                print(
                    f"CẢNH BÁO: Không tắt được relay khi cleanup: {error}",
                    file=sys.stderr,
                )
        client.close()


def interactive_mode() -> int:
    modbus_config, relay_config = load_configuration()
    print("Chế độ kiểm tra relay")
    print("  doc          - dò/đọc địa chỉ relay")
    print("  docrelay1-2  - đọc trạng thái hai relay (FC01, 8 coil)")
    print("  battat       - bật/tắt lần lượt hai relay")
    print("  thoat        - kết thúc")
    while True:
        try:
            command = input("Nhập chế độ: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if command in ("thoat", "exit", "quit", "q"):
            return 0
        if command == "doc":
            try:
                scan_relay_id(modbus_config, relay_config)
            except Exception as error:
                print(f"Đọc địa chỉ relay thất bại: {error}", file=sys.stderr)
            continue
        if command == "docrelay1-2":
            run_hardware_test(modbus_config, relay_config, check_only=True)
            continue
        if command == "battat":
            run_hardware_test(modbus_config, relay_config)
            continue
        print("Lựa chọn không hợp lệ. Nhập doc, docrelay1-2, battat hoặc thoat.")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return interactive_mode()

    parser = argparse.ArgumentParser(description="Kiểm tra và chạy chuỗi relay EPCB.")
    parser.add_argument(
        "--slave-id",
        type=int,
        default=None,
        help="Thử slave ID này thay cho relay.slave_id trong YAML.",
    )
    parser.add_argument(
        "--channel",
        type=int,
        choices=(1, 2),
        default=None,
        help="Chỉ đọc/test trạng thái riêng kênh này từ phản hồi FC01 8 coil.",
    )
    parser.add_argument(
        "--check-id-only",
        action="store_true",
        help="Chỉ đọc trạng thái FC01 để xác nhận ID; không bật/tắt relay.",
    )
    parser.add_argument(
        "--scan-baud-only",
        action="store_true",
        help="Chỉ thử đọc baud ở 4800/9600/19200 và ID mục tiêu/mặc định.",
    )
    parser.add_argument(
        "--scan-id-only",
        action="store_true",
        help="Chỉ đọc ID bằng FC03 broadcast ở các baud cấu hình; không bật/tắt relay.",
    )
    parser.add_argument(
        "--set-id",
        type=int,
        default=None,
        help="Đổi ID relay bằng FC16 broadcast rồi xác nhận lại; không bật/tắt relay.",
    )
    args = parser.parse_args(argv)

    modbus_config, relay_config = load_configuration()
    if args.slave_id is not None:
        relay_config = {**relay_config, "slave_id": args.slave_id}
    if args.set_id is not None:
        try:
            set_relay_id(modbus_config, relay_config, args.set_id)
            return 0
        except Exception as error:
            print(f"Đổi ID relay thất bại: {error}", file=sys.stderr)
            return 1
    if args.scan_id_only:
        try:
            scan_relay_id(modbus_config, relay_config)
            return 0
        except Exception as error:
            print(f"Dò ID relay thất bại: {error}", file=sys.stderr)
            return 1
    if args.scan_baud_only:
        try:
            scan_relay_baud(modbus_config, relay_config)
            return 0
        except Exception as error:
            print(f"Quét baud relay thất bại: {error}", file=sys.stderr)
            return 1

    return run_hardware_test(
        modbus_config,
        relay_config,
        channel=args.channel,
        check_only=args.check_id_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
