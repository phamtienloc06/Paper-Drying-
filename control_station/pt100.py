"""Driver PT100 dùng bus chung; trả mẫu mới hoặc lỗi, không điều khiển tải."""

from __future__ import annotations
from collections.abc import Mapping
import math
from typing import Any
from .modbus import ModbusRTU


class PT100DataError(ValueError):
    """Phản hồi sai định dạng hoặc nhiệt độ ngoài giới hạn cấu hình."""


def _integer(config: Mapping, key: str, minimum: int, maximum: int) -> int:
    value = config.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"PT100 {key} phải là số nguyên trong {minimum}..{maximum}.")
    return value


def decode_temperature(raw_register: int, data_type: str, scale: float) -> float:
    if type(raw_register) is not int or not 0 <= raw_register <= 0xFFFF:
        raise PT100DataError("Thanh ghi PT100 phải là số nguyên 16-bit không dấu.")
    if data_type not in ("int16", "uint16"):
        raise ValueError(f"PT100 data_type chưa hỗ trợ: {data_type}")
    if (
        isinstance(scale, bool)
        or not isinstance(scale, (int, float))
        or not math.isfinite(scale)
        or scale <= 0
    ):
        raise ValueError("PT100 scale phải hữu hạn và lớn hơn 0.")
    if data_type == "int16" and raw_register & 0x8000:
        raw_register -= 0x10000
    temperature = raw_register * scale
    if not math.isfinite(temperature):
        raise PT100DataError("Nhiệt độ giải mã không hữu hạn.")
    return float(temperature)


class PT100Sensor:
    """read_sample() trả (raw, °C); read_temperature() trả °C của mẫu mới."""

    def __init__(self, modbus: ModbusRTU, config: Mapping[str, Any]) -> None:
        self._modbus = modbus
        self._config = dict(config)
        self.slave_id = _integer(config, "slave_id", 1, 247)
        self.temperature_register = _integer(config, "temperature_register", 0, 65535)
        if (
            config.get("temperature_register_count", 1) != 1
            or config.get("temperature_function_code", 3) != 3
        ):
            raise ValueError("Driver PT100 chỉ hỗ trợ FC03, count=1.")
        self.data_type = config.get("data_type")
        self.scale = config.get("scale")
        decode_temperature(0, self.data_type, self.scale)
        self.minimum_temperature_c = config.get("minimum_temperature_c")
        self.maximum_temperature_c = config.get("maximum_temperature_c")
        for value in (self.minimum_temperature_c, self.maximum_temperature_c):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Giới hạn PT100 phải là số hữu hạn trong cấu hình.")
        if self.minimum_temperature_c >= self.maximum_temperature_c:
            raise ValueError("Cần minimum_temperature_c < maximum_temperature_c.")

    def read_sample(self) -> tuple[int, float]:
        values = self._modbus.read_holding_registers(
            slave_id=self.slave_id, address=self.temperature_register, count=1
        )
        if not isinstance(values, (list, tuple)) or len(values) != 1:
            raise PT100DataError("Phản hồi PT100 phải chứa đúng một thanh ghi.")
        raw = values[0]
        temperature = decode_temperature(raw, self.data_type, self.scale)
        if not self.minimum_temperature_c <= temperature <= self.maximum_temperature_c:
            raise PT100DataError(f"Nhiệt độ PT100 ngoài giới hạn: {temperature:.1f}°C.")
        return raw, temperature

    def read_temperature(self) -> float:
        return self.read_sample()[1]

    def change_slave_id(self, current_slave_id: int) -> bool:
        """Commissioning tường minh; không tự chạy khi khởi động."""
        if type(current_slave_id) is not int or not 1 <= current_slave_id <= 255:
            raise ValueError("current_slave_id phải trong 1..255.")
        address = _integer(self._config, "device_id_register", 0, 65535)
        if current_slave_id == self.slave_id:
            return False
        self._modbus.write_register(
            slave_id=current_slave_id, address=address, value=self.slave_id
        )
        return True
