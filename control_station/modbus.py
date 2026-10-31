"""Giao tiếp Modbus RTU; đây là module duy nhất sở hữu cổng COM.

Luồng sử dụng: tạo ModbusRTU từ cấu hình -> connect() mở COM -> gọi các hàm
read/write -> _execute() tuần tự hóa và retry giao dịch -> close() khi dừng.
PT100/relay truyền slave ID và địa chỉ thanh ghi vào đây; module không quyết
định chế độ điều khiển hay diễn giải giá trị nhiệt độ.

Trách nhiệm: sở hữu duy nhất cổng COM/USB–RS485, tuần tự hóa giao dịch,
xử lý timeout/retry/lỗi; HMI, MQTT, PT100 và relay không tự mở cổng serial.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import inspect
import logging
import threading
import time
from typing import Any, TypeVar


logger = logging.getLogger(__name__)


class ModbusCommunicationError(RuntimeError):
    """Lỗi giao tiếp sau khi giao dịch đã dùng hết số lần thử lại."""


class ModbusDeviceError(ModbusCommunicationError):
    """Thiết bị từ chối yêu cầu không hợp lệ; gửi lại không sửa được lỗi."""


ResponseT = TypeVar("ResponseT")


class ModbusRTU:
    """Quản lý một bus RTU chung và chỉ cho phép một giao dịch tại một thời điểm."""

    def __init__(self, config: Mapping[str, Any], client: Any | None = None) -> None:
        """Đọc thông số serial/retry; client tùy chọn để kiểm thử mà không cần COM."""
        self.port = str(config["port"])
        self.baudrate = int(config["baudrate"])
        self.parity = str(config.get("parity", "N")).upper()
        self.bytesize = int(config.get("bytesize", 8))
        self.stopbits = float(config.get("stopbits", 1))
        self.timeout_ms = int(config.get("timeout_ms", 1000))
        self.retries = int(config["retries"])  # Số lần thử lại sau lần gửi đầu tiên.
        self.retry_delay_ms = int(
            config["retry_delay_ms"]
        )  # Độ trễ thử lại lấy từ cấu hình.

        if not self.port:
            raise ValueError("Modbus serial port must not be empty.")
        if self.baudrate <= 0 or self.timeout_ms <= 0:
            raise ValueError("baudrate and timeout_ms must be positive.")
        if self.parity not in {"N", "E", "O"}:
            raise ValueError("parity must be N, E, or O.")
        if self.bytesize not in {5, 6, 7, 8} or self.stopbits not in {1, 1.5, 2}:
            raise ValueError("Invalid serial bytesize or stopbits.")
        if self.retries < 0 or self.retry_delay_ms < 0:
            raise ValueError("retries and retry_delay_ms cannot be negative.")

        self._client = client
        self._connected = False
        self._lock = threading.RLock()

    def connect(self) -> None:
        """Tạo PyModbus client lần đầu rồi mở COM; gọi lặp lại không mở thêm cổng."""
        with self._lock:
            if self._client is None:
                from pymodbus.client import ModbusSerialClient

                self._client = ModbusSerialClient(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=self.bytesize,
                    parity=self.parity,
                    stopbits=self.stopbits,
                    timeout=self.timeout_ms / 1000,
                    retries=0,
                )
            self._ensure_connected()

    def close(self) -> None:
        """Đóng client/cổng serial khi chương trình kết thúc."""
        with self._lock:
            try:
                if self._client is not None:
                    self._client.close()
            finally:
                self._connected = False

    def __enter__(self) -> ModbusRTU:
        """Cho phép dùng with để tự mở COM khi vào khối."""
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        """Tự đóng COM khi thoát khỏi khối with, kể cả khi có exception."""
        self.close()

    def read_holding_registers(
        self,
        slave_id: int,
        address: int,
        count: int = 1,
        allow_broadcast: bool = False,
    ) -> list[int]:
        """Gửi FC03; allow_broadcast chỉ dành cho truy vấn ID relay EPCB đã ghi rõ."""
        if slave_id == 0:
            if not allow_broadcast:
                raise ValueError("Broadcast read requires allow_broadcast=True.")
        else:
            self._validate_slave(slave_id)
        self._validate_range(address, count, maximum=125)
        response = self._execute(
            "read holding registers",
            lambda: self._call_client(
                "read_holding_registers", slave_id, address=address, count=count
            ),
            validate=lambda response: self._validate_payload(
                response, "registers", count
            ),
            slave_id=slave_id, function_code=0x03, address=address, count=count,
        )
        registers = list(response.registers)
        if len(registers) < count:
            raise ModbusCommunicationError(
                f"FC03 returned {len(registers)} registers; expected {count}."
            )
        return registers[:count]

    def read_input_registers(
        self, slave_id: int, address: int, count: int = 1
    ) -> list[int]:
        """Gửi FC04; dùng khi datasheet thiết bị khai báo input registers."""
        self._validate_slave(slave_id)
        self._validate_range(address, count, maximum=125)
        response = self._execute(
            "read input registers",
            lambda: self._call_client(
                "read_input_registers", slave_id, address=address, count=count
            ),
            validate=lambda response: self._validate_payload(
                response, "registers", count
            ),
            slave_id=slave_id, function_code=0x04, address=address, count=count,
        )
        registers = list(response.registers)
        if len(registers) < count:
            raise ModbusCommunicationError(
                f"FC04 returned {len(registers)} registers; expected {count}."
            )
        return registers[:count]

    def write_register(self, slave_id: int, address: int, value: int) -> None:
        """Gửi FC06 để ghi một holding register, ví dụ thanh ghi cấu hình thiết bị."""
        self._validate_slave(slave_id)
        self._validate_range(address, 1, maximum=1)
        if type(value) is not int or not 0 <= value <= 0xFFFF:
            raise ValueError("Register value must be between 0 and 65535.")
        self._execute(
            "write register",
            lambda: self._call_client(
                "write_register", slave_id, address=address, value=value
            ),
            slave_id=slave_id, function_code=0x06, address=address, count=1,
        )

    def write_registers(
        self,
        slave_id: int,
        address: int,
        values: Sequence[int],
        no_response_expected: bool = False,
    ) -> None:
        """Gửi FC16 để ghi các holding register liên tiếp.

        ID 0 chỉ được dùng khi gọi broadcast tường minh và không chờ phản hồi,
        ví dụ lệnh EPCB đổi ID relay tại register 0x0000.
        """
        if slave_id == 0:
            if not no_response_expected:
                raise ValueError(
                    "Broadcast slave ID 0 requires no_response_expected=True."
                )
        else:
            self._validate_slave(slave_id)
        self._validate_range(address, len(values), maximum=123)
        if any(type(value) is not int or not 0 <= value <= 0xFFFF for value in values):
            raise ValueError("All register values must be integers from 0 to 65535.")
        self._execute(
            "write registers",
            lambda: self._call_client(
                "write_registers",
                slave_id,
                address=address,
                values=list(values),
                no_response_expected=no_response_expected,
            ),
            expect_response=not no_response_expected,
            slave_id=slave_id, function_code=0x10, address=address, count=len(values),
        )

    def read_coils(
        self, slave_id: int, address: int, count: int = 1, *, retries: int | None = None
    ) -> list[bool]:
        """Gửi FC01 để đọc trạng thái coil, ví dụ trạng thái các relay."""
        self._validate_slave(slave_id)
        self._validate_range(address, count, maximum=2000)
        response = self._execute(
            "read coils",
            lambda: self._call_client(
                "read_coils", slave_id, address=address, count=count
            ),
            validate=lambda response: self._validate_payload(response, "bits", count),
            slave_id=slave_id, function_code=0x01, address=address, count=count,
            retries=retries,
        )
        bits = list(response.bits)
        if len(bits) < count:
            raise ModbusCommunicationError(
                f"FC01 returned fewer than {count} coil states."
            )
        return [bool(bit) for bit in bits[:count]]

    def write_coil(self, slave_id: int, address: int, enabled: bool) -> None:
        """Gửi FC05 để bật/tắt một coil relay tại địa chỉ được truyền vào."""
        self._validate_slave(slave_id)
        self._validate_range(address, 1, maximum=1)
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool.")
        self._execute(
            "write coil",
            lambda: self._call_client(
                "write_coil", slave_id, address=address, value=enabled
            ),
            slave_id=slave_id, function_code=0x05, address=address, count=1,
        )

    def write_coils(self, slave_id: int, address: int, values: Sequence[bool]) -> None:
        """Gửi FC0F để ghi nhiều coil liên tiếp trong một giao dịch."""
        self._validate_slave(slave_id)
        self._validate_range(address, len(values), maximum=1968)
        if any(type(value) is not bool for value in values):
            raise TypeError("All coil values must be bool.")
        self._execute(
            "write coils",
            lambda: self._call_client(
                "write_coils", slave_id, address=address, values=list(values)
            ),
            slave_id=slave_id, function_code=0x0F, address=address, count=len(values),
        )

    def _ensure_connected(self) -> None:
        """Đảm bảo COM đang mở trước mỗi giao dịch hoặc báo lỗi kết nối."""
        if self._client is None:
            raise ModbusCommunicationError("Modbus client has not been initialized.")
        if not self._connected:
            if not self._client.connect():
                raise ModbusCommunicationError(
                    f"Could not open serial port {self.port}."
                )
            self._connected = True

    def _call_client(self, method_name: str, slave_id: int, **kwargs: Any) -> Any:
        """Gọi PyModbus, tương thích tên tham số slave/device_id giữa các phiên bản."""
        if self._client is None:
            raise ModbusCommunicationError("Modbus client has not been initialized.")
        method = getattr(self._client, method_name)
        parameters = inspect.signature(method).parameters
        unit_name = next(
            (name for name in ("device_id", "slave", "unit") if name in parameters),
            "device_id",
        )
        return method(**kwargs, **{unit_name: slave_id})

    def _execute(
        self,
        operation: str,
        request: Callable[[], ResponseT],
        expect_response: bool = True,
        validate: Callable[[ResponseT], None] | None = None,
        *,
        slave_id: int,
        function_code: int,
        address: int,
        count: int,
        retries: int | None = None,
    ) -> ResponseT:
        """Khóa bus, gửi request, kiểm tra exception response và thử lại khi lỗi.

        retries=5 nghĩa là tối đa 6 lần gửi: một lần đầu và năm lần thử lại.
        """
        retry_count = self.retries if retries is None else retries
        if type(retry_count) is not int or retry_count < 0:
            raise ValueError("retries must be a non-negative integer.")
        with self._lock:
            context = (
                f"port={self.port} ID={slave_id} FC=0x{function_code:02X} "
                f"address=0x{address:04X} count={count}"
            )
            operation = f"{operation} ({context})"
            last_error: Exception | None = None
            for attempt in range(retry_count + 1):
                started_at = time.monotonic()
                logger.debug(
                    "Modbus request: %s attempt=%d/%d",
                    context, attempt + 1, retry_count + 1,
                )
                try:
                    self._ensure_connected()
                    response = request()
                    if not expect_response:
                        return response
                    if response is None:
                        raise ModbusCommunicationError(
                            f"Missing response during {operation}."
                        )
                    if response.isError():
                        code = getattr(response, "exception_code", None)
                        if code in (1, 2, 3):
                            raise ModbusDeviceError(
                                f"Device rejected {operation}; exception code {code}."
                            )
                        raise ModbusCommunicationError(
                            f"Modbus exception during {operation}."
                        )
                    if validate is not None:
                        validate(response)
                    if attempt:
                        logger.info(
                            "Modbus recovered: %s attempt=%d/%d",
                            context, attempt + 1, retry_count + 1,
                        )
                    return response
                except ModbusDeviceError as error:
                    logger.warning(
                        "Modbus request rejected; no retry: %s attempt=%d/%d "
                        "elapsed_ms=%.0f: %s",
                        context, attempt + 1, retry_count + 1,
                        (time.monotonic() - started_at) * 1000, error,
                    )
                    raise
                except Exception as error:
                    last_error = error
                    logger.warning(
                        "Modbus attempt failed: %s attempt=%d/%d elapsed_ms=%.0f: %s",
                        context, attempt + 1, retry_count + 1,
                        (time.monotonic() - started_at) * 1000, error,
                    )
                    self._close_after_failure()
                    if attempt < retry_count and self.retry_delay_ms:
                        time.sleep(self.retry_delay_ms / 1000)

            raise ModbusCommunicationError(
                f"Modbus {operation} failed after {retry_count + 1} attempts: {last_error}"
            ) from last_error

    def _close_after_failure(self) -> None:
        """Đóng kết nối lỗi để lần thử tiếp theo mở lại cổng sạch."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                # Lỗi đóng cổng không được che lỗi giao dịch hoặc ngắt retry.
                pass
            finally:
                self._connected = False

    @staticmethod
    def _validate_payload(response: Any, name: str, count: int) -> None:
        values = getattr(response, name, None)
        if values is None or len(values) < count:
            raise ModbusCommunicationError(
                f"Incomplete {name} response; expected {count}."
            )

    @staticmethod
    def _validate_slave(slave_id: int) -> None:
        """Chặn ID ngoài dải 1..255 mà hai thiết bị EPCB công bố hỗ trợ."""
        if type(slave_id) is not int or not 1 <= slave_id <= 255:
            raise ValueError("Modbus slave_id must be an integer from 1 to 255.")

    @staticmethod
    def _validate_range(address: int, count: int, maximum: int) -> None:
        """Kiểm tra địa chỉ, số lượng register/coil và giới hạn giao thức."""
        if type(address) is not int or not 0 <= address <= 0xFFFF:
            raise ValueError("Modbus address must be an integer from 0 to 65535.")
        if type(count) is not int or not 1 <= count <= maximum:
            raise ValueError(f"count must be between 1 and {maximum}.")
        if address + count > 0x10000:
            raise ValueError("Modbus address range exceeds 65535.")
