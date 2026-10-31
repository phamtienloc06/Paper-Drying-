"""Các kiểu dữ liệu dùng chung trong chương trình điều khiển máy 1."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
import math
from typing import TypeAlias
from uuid import uuid4


CommandValue: TypeAlias = bool | float | str | dict[str, float] | None


def _now_ms() -> int:
    """Trả về Unix timestamp UTC theo millisecond."""
    return time.time_ns() // 1_000_000


class ControlMode(str, Enum):
    """Các chế độ vận hành của hệ thống."""

    OFF = "OFF"
    MANUAL = "MANUAL"
    AUTO = "AUTO"
    FAULT = "FAULT"


class AutoPhase(str, Enum):
    """Pha hiện tại trong chu trình AUTO để controller và telemetry cùng dùng."""

    IDLE = "IDLE"
    HEATING = "HEATING"
    HOLDING = "HOLDING"
    COOLING = "COOLING"
    COMPLETE = "COMPLETE"


class CommandType(str, Enum):
    """Các loại yêu cầu gửi tới bộ điều khiển."""

    SET_MODE = "SET_MODE"
    SET_TEMPERATURE = "SET_TEMPERATURE"
    SET_DRYING_DURATION = "SET_DRYING_DURATION"
    SET_COOLING_TEMPERATURE = "SET_COOLING_TEMPERATURE"
    SET_RECIPE = "SET_RECIPE"
    START_DRYING = "START_DRYING"
    SET_HEATER = "SET_HEATER"
    SET_PROCESS_FAN = "SET_PROCESS_FAN"
    RESET_ALARM = "RESET_ALARM"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class CommandSource(str, Enum):
    """Nguồn yêu cầu; REMOTE_SCADA được controller áp dụng quyền hạn riêng."""

    LOCAL_HMI = "LOCAL_HMI"
    REMOTE_SCADA = "REMOTE_SCADA"
    SYSTEM = "SYSTEM"


class AlarmCode(str, Enum):
    """Các mã cảnh báo của hệ thống."""

    NONE = "NONE"
    SENSOR_DISCONNECTED = "SENSOR_DISCONNECTED"
    MODBUS_COMMUNICATION_LOST = "MODBUS_COMMUNICATION_LOST"
    OVER_TEMPERATURE = "OVER_TEMPERATURE"
    HEATER_CONTROL_FAILED = "HEATER_CONTROL_FAILED"
    PROCESS_FAN_START_FAILED = "PROCESS_FAN_START_FAILED"
    INVALID_COMMAND = "INVALID_COMMAND"
    EMERGENCY_STOPPED = "EMERGENCY_STOPPED"


class AlarmSeverity(str, Enum):
    """Mức độ nghiêm trọng của cảnh báo."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class SystemState:
    """Trạng thái đo lường, đầu ra, pha AUTO và kết nối hiện tại."""

    temperature_c: float | None = None
    heater_on: bool = False
    process_fan_on: bool = False
    auto_phase: AutoPhase = AutoPhase.IDLE
    drying_elapsed_s: float = 0.0
    drying_remaining_s: float = 0.0
    hold_timer_running: bool = False
    mode: ControlMode = ControlMode.OFF
    alarm_code: AlarmCode = AlarmCode.NONE
    sensor_connected: bool = False
    modbus_connected: bool = False
    mqtt_connected: bool = False
    timestamp_ms: int = field(default_factory=_now_ms)


@dataclass
class ControlSettings:
    """Thông số mẻ sấy và liên động; YAML cung cấp giá trị khi khởi động."""

    temperature_setpoint_c: float
    control_deadband_c: float
    high_temperature_limit_c: float
    minimum_setpoint_c: float
    maximum_setpoint_c: float
    heater_to_fan_start_delay_s: float
    drying_duration_s: float
    cooling_stop_temperature_c: float
    fan_start_confirmation_timeout_s: float

    def __post_init__(self) -> None:
        """Kiểm tra các thông số ngay khi tạo đối tượng."""
        self.validate()

    def validate(self) -> None:
        """Kiểm tra thông số nạp từ cấu hình hoặc caller; không giữ mặc định trùng YAML."""
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} phải là số hữu hạn.")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{name} phải là số hữu hạn.")
        if (
            not self.minimum_setpoint_c
            < self.maximum_setpoint_c
            < self.high_temperature_limit_c
        ):
            raise ValueError(
                "Cần minimum_setpoint < maximum_setpoint < high_temperature_limit."
            )
        if (
            not self.minimum_setpoint_c
            <= self.temperature_setpoint_c
            <= self.maximum_setpoint_c
        ):
            raise ValueError("Setpoint nằm ngoài giới hạn cấu hình.")
        if self.control_deadband_c <= 0:
            raise ValueError("Deadband phải lớn hơn 0.")
        if self.heater_to_fan_start_delay_s < 0:
            raise ValueError("Thời gian trễ bật quạt không được âm.")
        if self.drying_duration_s <= 0:
            raise ValueError("Thời gian giữ nhiệt để sấy phải lớn hơn 0.")
        if (
            not 0
            <= self.cooling_stop_temperature_c
            < (self.temperature_setpoint_c - self.control_deadband_c)
        ):
            raise ValueError("Ngưỡng nguội phải >= 0 và thấp hơn setpoint - deadband.")
        if (
            self.temperature_setpoint_c + self.control_deadband_c
            >= self.high_temperature_limit_c
        ):
            raise ValueError("Vùng giữ nhiệt phải thấp hơn giới hạn quá nhiệt.")
        if self.fan_start_confirmation_timeout_s <= 0:
            raise ValueError("Hạn xác nhận quạt phải lớn hơn 0.")


@dataclass
class ControlCommand:
    """Yêu cầu điều khiển kèm nguồn gửi và định danh riêng."""

    command_type: CommandType
    source: CommandSource
    value: CommandValue = None
    command_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp_ms: int = field(default_factory=_now_ms)


@dataclass
class CommandResult:
    """Kết quả xử lý gắn với định danh của yêu cầu điều khiển."""

    command_id: str
    accepted: bool
    message: str
    timestamp_ms: int = field(default_factory=_now_ms)


@dataclass
class Alarm:
    """Thông tin cảnh báo và thời điểm phát sinh, kết thúc."""

    code: AlarmCode
    severity: AlarmSeverity
    message: str
    alarm_id: str = field(default_factory=lambda: str(uuid4()))
    active: bool = True
    raised_at_ms: int = field(default_factory=_now_ms)
    cleared_at_ms: int | None = None


@dataclass
class TelemetryRecord:
    """Bản ghi đo lường, đầu ra, pha điều khiển và kết nối tại một thời điểm."""

    temperature_c: float | None
    temperature_setpoint_c: float
    heater_on: bool
    process_fan_on: bool
    auto_phase: AutoPhase
    drying_elapsed_s: float
    drying_remaining_s: float
    hold_timer_running: bool
    drying_duration_s: float
    cooling_stop_temperature_c: float
    mode: ControlMode
    alarm_code: AlarmCode
    sensor_connected: bool
    modbus_connected: bool
    timestamp_ms: int = field(default_factory=_now_ms)
