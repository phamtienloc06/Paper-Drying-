"""Điều phối chế độ, lệnh và an toàn qua giao diện relay độc lập phần cứng."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
import threading
import time
from typing import Callable, Protocol

from .models import (
    Alarm,
    AlarmCode,
    AlarmSeverity,
    AutoPhase,
    CommandResult,
    CommandSource,
    CommandType,
    ControlCommand,
    ControlMode,
    ControlSettings,
    SystemState,
)


class RelayPort(Protocol):
    """Giao diện relay; thành công khi lời gọi không phát sinh exception."""

    def set_heater(self, enabled: bool) -> None:
        """Yêu cầu bật hoặc tắt máy sưởi."""
        ...

    def set_process_fan(self, enabled: bool) -> None:
        """Yêu cầu bật hoặc tắt quạt quá trình."""
        ...

    def all_outputs_off(self) -> None:
        """Yêu cầu tắt toàn bộ đầu ra."""
        ...


class SystemController:
    """Quản lý dữ liệu nội bộ và tuần tự hóa lệnh cùng chu kỳ điều khiển."""

    # Khởi tạo trạng thái, thông số điều khiển, cổng relay và đồng hồ đơn điệu.
    def __init__(
        self,
        state: SystemState,
        settings: ControlSettings,
        relay: RelayPort,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Sao chép dữ liệu đầu vào, chưa giao tiếp phần cứng."""
        settings.validate()
        self._state = deepcopy(state)
        self._settings = deepcopy(settings)
        # Không tiếp tục một chu trình AUTO/MANUAL đang dở sau khi tạo lại
        # controller; lỗi được truyền vào thì vẫn giữ trạng thái khóa FAULT.
        self._state.mode = (
            ControlMode.FAULT
            if self._state.alarm_code != AlarmCode.NONE
            else ControlMode.OFF
        )
        # Lưu relay và các tham số điều khiển thời gian.
        self._relay = relay
        self._heater_to_fan_start_delay_s = float(settings.heater_to_fan_start_delay_s)
        self._fan_start_confirmation_timeout_s = float(
            settings.fan_start_confirmation_timeout_s
        )
        self._clock = clock or time.monotonic
        self._active_alarm: Alarm | None = None
        self._lock = threading.RLock()
        self._needs_output_sync = True
        self._emergency_stopped = state.alarm_code == AlarmCode.EMERGENCY_STOPPED
        self._auto_phase = AutoPhase.IDLE
        self._auto_phase_started_at: float | None = None
        self._hold_last_tick: float | None = None
        self._hold_previous_in_band = False
        # Thời điểm pha chỉ tồn tại trong lần chạy hiện tại; restart không khôi
        # phục bộ đếm cũ mà bắt đầu ở OFF và đồng bộ relay.
        self._cancel_auto_sequence()

    def update_measurement(
        self,
        temperature_c: float | None,
        sensor_connected: bool,
        modbus_connected: bool,
    ) -> None:
        """Cập nhật mẫu đo; kết nối lại cần đồng bộ relay nhưng không xóa FAULT."""
        with self._lock:
            was_modbus_connected = self._state.modbus_connected
            if modbus_connected and not was_modbus_connected:
                self._needs_output_sync = True
            elif was_modbus_connected and not modbus_connected:
                self._needs_output_sync = True
                self._cancel_auto_sequence()
            self._state.temperature_c = temperature_c
            self._state.sensor_connected = sensor_connected
            self._state.modbus_connected = modbus_connected
            self._touch_state()

    def update_mqtt_connection(self, connected: bool) -> None:
        """Cập nhật riêng MQTT, không ảnh hưởng điều khiển cục bộ.
        Ở đây chỉ để telemetry/UI biết trạng thái MQTT, không ảnh hướng đến điều khiển
        ở máy 2. Nếu mất MQTT thì vẫn giữ chế độ cục bộ, chỉ cảnh báo UI."""
        with self._lock:
            self._state.mqtt_connected = connected

    def run_control_cycle(self) -> None:
        """Kiểm tra an toàn, đồng bộ phần cứng rồi xử lý chế độ."""
        with self._lock:
            self._touch_state()
            # Kiểm tra bus, cảm biến, quá nhiệt trước mọi yêu cầu đầu ra.
            self._evaluate_safety()
            if not self._synchronize_outputs():
                return
            if self._state.mode == ControlMode.FAULT:
                self._apply_fault_outputs()
            elif self._state.mode == ControlMode.OFF:
                self._all_outputs_off()
            elif self._state.mode == ControlMode.AUTO:
                self._update_auto_control()
            # MANUAL giữ đầu ra đã kiểm tra, không áp dụng setpoint.

    def process_command(self, command: ControlCommand) -> CommandResult:
        """Trả kết quả xử lý hoặc lý do từ chối khi dữ liệu lệnh sai."""
        with self._lock:
            if not isinstance(command, ControlCommand):
                return CommandResult("", False, "INVALID_COMMAND: Sai cấu trúc lệnh.")
            try:
                kind = CommandType(command.command_type)
                source = CommandSource(command.source)
            except (TypeError, ValueError):
                return self._reject_command(
                    command, "Loại lệnh hoặc nguồn không hợp lệ."
                )
            # dừng khẩn cấp
            # Không để timestamp hay payload vô hiệu hóa lệnh dừng khẩn.
            if kind == CommandType.EMERGENCY_STOP:
                self._emergency_stopped = True
                self._enter_fault(AlarmCode.EMERGENCY_STOPPED, "Dừng khẩn cấp.")
                stopped = self._all_outputs_off(force=True)
                return self._accept_command(
                    command,
                    "Đã dừng khẩn cấp."
                    if stopped
                    else "Đã khóa FAULT; chưa xác nhận đầu ra đã tắt.",
                )

            # SCADA chỉ gửi yêu cầu cấp cao; chỉ HMI tại chỗ mới được reset FAULT.
            if kind == CommandType.RESET_ALARM and source != CommandSource.LOCAL_HMI:
                return self._reject_command(command, "Chỉ LOCAL_HMI được reset FAULT.")
            if source == CommandSource.REMOTE_SCADA:
                if kind not in (
                    CommandType.SET_MODE,
                    CommandType.SET_TEMPERATURE,
                    CommandType.SET_DRYING_DURATION,
                    CommandType.SET_COOLING_TEMPERATURE,
                    CommandType.SET_RECIPE,
                    CommandType.START_DRYING,
                ):
                    return self._reject_command(
                        command, "SCADA không được điều khiển relay trực tiếp."
                    )
                if not self._is_number(command.timestamp_ms):
                    return self._reject_command(command, "Timestamp không hợp lệ.")
                if time.time_ns() // 1_000_000 - command.timestamp_ms > 5_000:
                    return self._reject_command(
                        command, "Lệnh SCADA đã hết hạn 5 giây."
                    )

            setting_fields = {
                CommandType.SET_TEMPERATURE: "temperature_setpoint_c",
                CommandType.SET_DRYING_DURATION: "drying_duration_s",
                CommandType.SET_COOLING_TEMPERATURE: "cooling_stop_temperature_c",
            }
            if kind in setting_fields or kind == CommandType.SET_RECIPE:
                if self._batch_running():
                    return self._reject_command(
                        command, "Mẻ sấy đang chạy; dừng mẻ trước khi chỉnh thông số."
                    )
                if kind == CommandType.SET_RECIPE:
                    fields = set(setting_fields.values())
                    if (
                        type(command.value) is not dict
                        or set(command.value) != fields
                        or not all(self._is_number(value) for value in command.value.values())
                    ):
                        return self._reject_command(command, "Bộ thông số phải gồm đúng ba giá trị số hữu hạn.")
                    updates = {key: float(value) for key, value in command.value.items()}
                else:
                    if not self._is_number(command.value):
                        return self._reject_command(
                            command, "Thông số phải là số hữu hạn, không nhận bool."
                        )
                    updates = {setting_fields[kind]: float(command.value)}
                try:
                    self._settings = replace(
                        self._settings, **updates
                    )
                except ValueError as error:
                    return self._reject_command(command, str(error))
                if self._auto_phase == AutoPhase.COMPLETE:
                    self._state.mode = ControlMode.OFF
                self._cancel_auto_sequence()
                return self._accept_command(
                    command, "Đã lưu thông số cho mẻ tiếp theo; chưa tác động đầu ra."
                )
            if kind == CommandType.START_DRYING:
                if command.value is not None:
                    return self._reject_command(
                        command, "START_DRYING không nhận giá trị."
                    )
                return self._change_mode(command, ControlMode.AUTO)
            if kind == CommandType.SET_MODE:
                try:
                    mode = ControlMode(command.value)
                except (TypeError, ValueError):
                    return self._reject_command(command, "Mode không hợp lệ.")
                if mode == ControlMode.FAULT:
                    return self._reject_command(
                        command, "Không được yêu cầu trực tiếp mode FAULT."
                    )
                if source == CommandSource.REMOTE_SCADA and mode == ControlMode.MANUAL:
                    return self._reject_command(
                        command, "SCADA chỉ được chọn AUTO hoặc OFF."
                    )
                return self._change_mode(command, mode)
            if kind == CommandType.RESET_ALARM:
                if command.value is not None:
                    return self._reject_command(
                        command, "RESET_ALARM không nhận giá trị."
                    )
                return self._reset_fault(command)
            if type(command.value) is not bool:
                return self._reject_command(
                    command, "Giá trị đầu ra phải chính xác là bool."
                )
            if self._state.mode != ControlMode.MANUAL:
                return self._reject_command(
                    command, "Chỉ điều khiển đầu ra trong MANUAL."
                )
            if not self._prepare_safe_operation():
                return self._reject_command(
                    command, "Hệ thống không an toàn hoặc relay bị lỗi."
                )
            if kind == CommandType.SET_HEATER:
                success = self._set_output("heater_on", command.value)
            elif kind == CommandType.SET_PROCESS_FAN:
                success = self._set_output("process_fan_on", command.value)
            else:
                return self._reject_command(command, "Loại lệnh không hỗ trợ.")
            if not success:
                return self._reject_command(command, "Không thực hiện được lệnh relay.")
            return self._accept_command(command, "Đã cập nhật đầu ra thủ công.")

    def shutdown(self) -> bool:
        """Hủy timer và cố gắng xác nhận hai tải OFF khi dừng chương trình."""
        with self._lock:
            self._cancel_auto_sequence()
            if self._state.mode != ControlMode.FAULT:
                self._state.mode = ControlMode.OFF
            return self._all_outputs_off(force=True)

    def get_state_snapshot(self) -> SystemState:
        """Trả bản sao độc lập của trạng thái hệ thống."""
        with self._lock:
            return deepcopy(self._state)

    def get_settings_snapshot(self) -> ControlSettings:
        """Trả bản sao độc lập của thông số điều khiển."""
        with self._lock:
            return deepcopy(self._settings)

    def get_active_alarm(self) -> Alarm | None:
        """Trả bản sao cảnh báo đang hoạt động hoặc None."""
        with self._lock:
            return deepcopy(self._active_alarm)

    @staticmethod
    def _is_number(value: object) -> bool:
        """Loại bool, số không hữu hạn và số nguyên quá lớn."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            return False

    def _valid_setpoint(self, value: object) -> bool:
        """Kiểm tra setpoint có nằm trong giới hạn cấu hình hay không."""
        return (
            self._is_number(value)
            and self._settings.minimum_setpoint_c
            <= value
            <= self._settings.maximum_setpoint_c
        )

    def _batch_running(self) -> bool:
        """Cho biết mẻ AUTO đang chạy và chưa ở pha hoàn tất."""
        return (
            self._state.mode == ControlMode.AUTO
            and self._auto_phase != AutoPhase.COMPLETE
        )

    def _evaluate_safety(self) -> bool:
        """Ghi nhận lỗi theo ưu tiên; dữ liệu phục hồi không tự xóa FAULT."""
        if not self._state.modbus_connected:
            self._needs_output_sync = True
            self._enter_fault(
                AlarmCode.MODBUS_COMMUNICATION_LOST, "Mất kết nối Modbus."
            )
            return False
        if not self._state.sensor_connected or not self._is_number(
            self._state.temperature_c
        ):
            self._enter_fault(
                AlarmCode.SENSOR_DISCONNECTED, "Cảm biến hoặc nhiệt độ không hợp lệ."
            )
            return False
        if self._state.temperature_c >= self._settings.high_temperature_limit_c:
            self._enter_fault(
                AlarmCode.OVER_TEMPERATURE, "Nhiệt độ đạt giới hạn quá nhiệt."
            )
            return False
        return True

    def _prepare_safe_operation(self) -> bool:
        """Dùng chung kiểm tra an toàn cho lệnh và chu kỳ điều khiển."""
        safe = self._evaluate_safety()
        if not self._synchronize_outputs():
            return False
        if not safe or self._state.mode == ControlMode.FAULT:
            self._apply_fault_outputs()
            return False
        return True

    def _synchronize_outputs(self) -> bool:
        """Force OFF khi kết nối vì trạng thái phần cứng có thể đã khác."""
        if not self._state.modbus_connected:
            return False
        return not self._needs_output_sync or self._all_outputs_off(force=True)

    def _enter_fault(self, code: AlarmCode, message: str) -> None:
        """Giữ FAULT, không tự khôi phục chế độ vận hành trước lỗi."""
        self._state.mode = ControlMode.FAULT
        self._cancel_auto_sequence()
        # Dừng khẩn phải được giữ đến khi HMI reset, kể cả khi phát sinh lỗi khác.
        if self._emergency_stopped:
            code = AlarmCode.EMERGENCY_STOPPED
            message = "Dừng khẩn cấp; cần LOCAL_HMI reset sau khi an toàn."
        self._state.alarm_code = code
        if self._active_alarm is None or self._active_alarm.code != code:
            self._active_alarm = Alarm(code, AlarmSeverity.CRITICAL, message)
        self._touch_state()

    def _apply_fault_outputs(self) -> None:
        """Khóa sưởi; hỗ trợ thoát nhiệt khi lỗi cảm biến hoặc quá nhiệt."""
        if not self._state.modbus_connected:
            return
        if not self._emergency_stopped and self._state.alarm_code in (
            AlarmCode.SENSOR_DISCONNECTED,
            AlarmCode.OVER_TEMPERATURE,
        ):
            if not self._set_output("heater_on", False):
                return
            self._set_output("process_fan_on", True)
        else:
            self._all_outputs_off()

    def _relay_failed(
        self,
        error: Exception,
        *,
        code: AlarmCode = AlarmCode.HEATER_CONTROL_FAILED,
        message: str | None = None,
    ) -> None:
        """Giữ FAULT và thử tắt một lần, tránh đệ quy nếu relay tiếp tục lỗi."""
        self._needs_output_sync = True
        self._enter_fault(
            code,
            message or f"Lỗi relay: {error}",
        )
        try:
            self._relay.all_outputs_off()
        except Exception:
            # Giữ lần xác nhận cuối, không giả định tải thật đã tắt khi mất phản hồi.
            return
        self._state.heater_on = False
        self._state.process_fan_on = False
        self._needs_output_sync = False

    def _all_outputs_off(self, force: bool = False) -> bool:
        """Chỉ xác nhận OFF sau thành công; tránh ghi lặp không cần thiết."""
        if not self._state.modbus_connected:
            self._needs_output_sync = True
            return False
        if not (
            force
            or self._needs_output_sync
            or self._state.heater_on
            or self._state.process_fan_on
        ):
            return True
        try:
            self._relay.all_outputs_off()
        except Exception as error:
            self._relay_failed(error)
            return False
        self._state.heater_on = False
        self._state.process_fan_on = False
        self._needs_output_sync = False
        self._touch_state()
        return True

    def _set_output(self, name: str, enabled: bool) -> bool:
        """Gửi lệnh một đầu ra và chỉ cập nhật state sau khi relay xác nhận."""
        if getattr(self._state, name) == enabled:
            return True
        try:
            self._write_output(name, enabled)
        except Exception as error:
            self._relay_failed(error)
            return False
        return True

    def _update_auto_control(self) -> None:
        """Một mẻ: gia nhiệt -> giữ đủ thời gian trong vùng -> nguội -> hoàn tất."""
        temperature = self._state.temperature_c
        setpoint = self._settings.temperature_setpoint_c
        lower = setpoint - self._settings.control_deadband_c
        upper = setpoint + self._settings.control_deadband_c
        now = self._clock()

        if self._auto_phase == AutoPhase.COMPLETE:
            # Hoàn tất là trạng thái chốt, không bắt đầu mẻ mới khi nhiệt giảm.
            return
        if self._auto_phase == AutoPhase.IDLE:
            if temperature >= setpoint:
                self._begin_holding()
            elif self._set_output("heater_on", True):
                self._set_auto_phase(AutoPhase.HEATING, self._clock())
                if self._heater_to_fan_start_delay_s == 0:
                    self._start_fan_with_retry()
            return
        if self._auto_phase == AutoPhase.HEATING:
            if temperature >= setpoint:
                self._begin_holding()
                return
            if not self._set_output("heater_on", True):
                return
            if (
                not self._state.process_fan_on
                and self._auto_phase_started_at is not None
                and now - self._auto_phase_started_at
                >= self._heater_to_fan_start_delay_s
            ):
                self._start_fan_with_retry()
            return
        if self._auto_phase == AutoPhase.HOLDING:
            in_band = lower <= temperature <= upper
            # Chỉ cộng khoảng giữa hai mẫu đều trong vùng. Khi ra/vào vùng,
            # bỏ khoảng chuyển tiếp; không đoán thời điểm nhiệt vượt ngưỡng.
            if (
                in_band
                and self._hold_previous_in_band
                and self._hold_last_tick is not None
            ):
                elapsed = max(0.0, now - self._hold_last_tick)
                self._state.drying_elapsed_s = min(
                    self._settings.drying_duration_s,
                    self._state.drying_elapsed_s + elapsed,
                )
            self._hold_last_tick = now
            self._hold_previous_in_band = in_band
            remaining = self._settings.drying_duration_s - self._state.drying_elapsed_s
            self._state.drying_remaining_s = max(0.0, remaining)
            self._state.hold_timer_running = in_band
            if remaining <= 1e-9:
                self._state.drying_elapsed_s = self._settings.drying_duration_s
                self._state.drying_remaining_s = 0.0
                if self._set_output("heater_on", False):
                    self._state.hold_timer_running = False
                    self._set_auto_phase(AutoPhase.COOLING)
                    self._finish_cooling_if_ready()
                return
            # Quạt giữ ON; chỉ heater đóng/ngắt theo hysteresis S-D / S.
            if temperature >= setpoint:
                self._set_output("heater_on", False)
            elif temperature <= lower:
                self._set_output("heater_on", True)
            return
        if self._auto_phase == AutoPhase.COOLING:
            # Hết giờ sấy không bật heater lại, kể cả nhiệt tụt dưới S-D.
            if self._set_output("heater_on", False):
                self._finish_cooling_if_ready()

    def _begin_holding(self) -> None:
        """Đạt điểm đặt: tắt heater, xác nhận quạt ON rồi mới bắt đầu bộ đếm."""
        if not self._set_output("heater_on", False):
            return
        if not self._state.process_fan_on and not self._start_fan_with_retry():
            return
        self._set_auto_phase(AutoPhase.HOLDING)
        self._hold_last_tick = self._clock()
        self._hold_previous_in_band = (
            self._settings.temperature_setpoint_c - self._settings.control_deadband_c
            <= self._state.temperature_c
            <= self._settings.temperature_setpoint_c + self._settings.control_deadband_c
        )
        self._state.hold_timer_running = self._hold_previous_in_band

    def _finish_cooling_if_ready(self) -> None:
        """Kết thúc mẻ khi nhiệt độ đã xuống dưới ngưỡng nguội cài đặt."""
        if self._state.temperature_c <= self._settings.cooling_stop_temperature_c:
            if self._set_output("process_fan_on", False):
                self._set_auto_phase(AutoPhase.COMPLETE)

    def _write_output(self, name: str, enabled: bool, *, force: bool = False) -> None:
        """Ghi một đầu ra relay; lỗi được đưa về caller để chọn chính sách."""
        if not force and getattr(self._state, name) == enabled:
            return
        if name == "heater_on":
            self._relay.set_heater(enabled)
        else:
            self._relay.set_process_fan(enabled)
        setattr(self._state, name, enabled)
        self._touch_state()

    def _start_fan_with_retry(self) -> bool:
        """Xác nhận bật quạt trong hạn; nếu trễ/lỗi thì ngắt heater và thử lại."""
        first_failure: Exception | None = None
        attempt_started_at = self._clock()
        try:
            self._write_output("process_fan_on", True, force=True)
            elapsed = self._clock() - attempt_started_at
            if elapsed <= self._fan_start_confirmation_timeout_s:
                return True
            first_failure = TimeoutError(
                "Process fan confirmation exceeded "
                f"{self._fan_start_confirmation_timeout_s:g} seconds."
            )
        except Exception as error:
            first_failure = error

        # Ghi/đọc relay là đồng bộ nên chỉ kiểm tra được thời hạn sau khi lời gọi trả về.
        if not self._set_output("heater_on", False):
            return False

        retry_started_at = self._clock()
        try:
            # Buộc ghi/đọc xác nhận lại kể cả khi lần đầu báo ON muộn và đã cập nhật cache.
            self._write_output("process_fan_on", True, force=True)
            elapsed = self._clock() - retry_started_at
            if elapsed > self._fan_start_confirmation_timeout_s:
                raise TimeoutError(
                    "Process fan confirmation retry exceeded "
                    f"{self._fan_start_confirmation_timeout_s:g} seconds."
                )
            # Đã xác nhận quạt; heater giữ OFF trong chu kỳ này. Pha HEATING
            # chỉ cho bật lại ở chu kỳ sau, khi an toàn được kiểm tra lại.
            return True
        except Exception as retry_error:
            self._relay_failed(
                retry_error,
                code=AlarmCode.PROCESS_FAN_START_FAILED,
                message=(
                    "Không xác nhận được process_fan ON sau một lần thử lại "
                    f"(lần đầu: {first_failure}; thử lại: {retry_error})."
                ),
            )
            return False

    def _set_auto_phase(
        self, phase: AutoPhase, started_at: float | None = None
    ) -> None:
        """Đồng bộ pha nội bộ và pha public để telemetry/UI đọc cùng một trạng thái."""
        self._auto_phase = phase
        self._auto_phase_started_at = started_at
        self._state.auto_phase = phase

    def _cancel_auto_sequence(self) -> None:
        """Xóa pha và mốc thời gian AUTO khi rời AUTO hoặc phát sinh lỗi."""
        self._set_auto_phase(AutoPhase.IDLE)
        self._hold_last_tick = None
        self._hold_previous_in_band = False
        self._state.drying_elapsed_s = 0.0
        self._state.drying_remaining_s = self._settings.drying_duration_s
        self._state.hold_timer_running = False

    def _change_mode(self, command: ControlCommand, mode: ControlMode) -> CommandResult:
        """Chuyển chế độ qua đầu ra OFF để không mang lệnh cũ sang mode mới."""
        if self._state.mode == ControlMode.FAULT:
            return self._reject_command(command, "Phải reset FAULT trước khi đổi mode.")
        if mode == ControlMode.AUTO and self._batch_running():
            return self._reject_command(
                command, "Mẻ sấy đang chạy; không được khởi động lại bộ đếm."
            )
        if not self._prepare_safe_operation():
            return self._reject_command(
                command, "Không thể đổi mode khi hệ thống không an toàn."
            )
        if mode == ControlMode.AUTO and not self._valid_setpoint(
            self._settings.temperature_setpoint_c
        ):
            return self._reject_command(command, "Setpoint không hợp lệ cho AUTO.")
        # Chuyển mode qua OFF để AUTO/MANUAL không kế thừa đầu ra cũ.
        if not self._all_outputs_off():
            return self._reject_command(command, "Không thể tắt đầu ra để đổi mode.")
        self._cancel_auto_sequence()
        self._state.mode = mode
        return self._accept_command(command, f"Đã chuyển sang {mode.value}.")

    def _reset_fault(self, command: ControlCommand) -> CommandResult:
        """Xóa lỗi khi dữ liệu an toàn và phần cứng xác nhận toàn bộ OFF."""
        if self._state.mode != ControlMode.FAULT:
            return self._reject_command(command, "Hệ thống không ở FAULT.")
        if (
            not self._state.modbus_connected
            or not self._state.sensor_connected
            or not self._is_number(self._state.temperature_c)
            or self._state.temperature_c
            > (
                self._settings.high_temperature_limit_c
                - self._settings.control_deadband_c
            )
        ):
            return self._reject_command(
                command, "Nguyên nhân lỗi chưa hết hoặc nhiệt độ chưa đủ thấp."
            )
        if not self._all_outputs_off(force=True):
            return self._reject_command(
                command, "Relay chưa xác nhận OFF; không thể reset."
            )
        if self._active_alarm is not None:
            self._active_alarm.active = False
            self._active_alarm.cleared_at_ms = time.time_ns() // 1_000_000
        self._active_alarm = None
        self._emergency_stopped = False
        self._cancel_auto_sequence()
        self._state.alarm_code = AlarmCode.NONE
        self._state.mode = ControlMode.OFF
        return self._accept_command(command, "Đã xóa lỗi và chuyển về OFF.")

    def _touch_state(self) -> None:
        """Cập nhật timestamp của snapshot trạng thái controller."""
        self._state.timestamp_ms = time.time_ns() // 1_000_000

    def _accept_command(self, command: ControlCommand, message: str) -> CommandResult:
        """Tạo kết quả chấp nhận lệnh, kèm trạng thái mới nhất."""
        self._touch_state()
        return CommandResult(command.command_id, True, message)

    def _reject_command(self, command: ControlCommand, message: str) -> CommandResult:
        """Tạo kết quả từ chối lệnh và giữ nguyên trạng thái an toàn hiện tại."""
        return CommandResult(command.command_id, False, f"INVALID_COMMAND: {message}")
