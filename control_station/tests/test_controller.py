"""Kiểm thử chế độ OFF qua public API, không sử dụng phần cứng thật."""

from __future__ import annotations

import unittest
from control_station.tests.helpers import make_settings

from control_station.controller import SystemController
from control_station.models import (
    AlarmCode,
    AutoPhase,
    CommandSource,
    CommandType,
    ControlCommand,
    ControlMode,
    SystemState,
)


class FakeRelay:
    """Mô phỏng đầu ra và lưu lịch sử lệnh để quan sát tác động của controller."""

    def __init__(self, heater_on: bool = False, process_fan_on: bool = False) -> None:
        """Cho phép giả lập đầu ra còn bật từ trước khi controller khởi động."""
        self.heater_on = heater_on
        self.process_fan_on = process_fan_on
        self.command_history: list[tuple[str, bool | None]] = []
        self.fail_operations: set[str] = set()
        self.fail_next_operations: dict[str, int] = {}

    def _record_operation(self, operation: str, enabled: bool | None = None) -> None:
        """Ghi cả yêu cầu thất bại; lỗi xảy ra trước khi đầu ra giả được cập nhật."""
        self.command_history.append((operation, enabled))
        failures_left = self.fail_next_operations.get(operation, 0)
        if failures_left:
            self.fail_next_operations[operation] = failures_left - 1
            raise RuntimeError(f"Lỗi relay giả tại {operation}")
        if operation in self.fail_operations:
            raise RuntimeError(f"Lỗi relay giả tại {operation}")

    def set_heater(self, enabled: bool) -> None:
        """Cập nhật máy sưởi giả và ghi nhận lệnh tương ứng."""
        self._record_operation("set_heater", enabled)
        self.heater_on = enabled

    def set_process_fan(self, enabled: bool) -> None:
        """Cập nhật quạt quá trình giả và ghi nhận lệnh tương ứng."""
        self._record_operation("set_process_fan", enabled)
        self.process_fan_on = enabled

    def all_outputs_off(self) -> None:
        """Tắt cả hai đầu ra giả và ghi nhận yêu cầu đồng bộ OFF."""
        self._record_operation("all_outputs_off")
        self.heater_on = False
        self.process_fan_on = False


class TestSystemControllerOffMode(unittest.TestCase):
    """Xác minh OFF giữ đầu ra tắt nhưng vẫn tiếp nhận thông số và phép đo."""

    def setUp(self) -> None:
        """Tạo dữ liệu và relay riêng để mỗi test không phụ thuộc test khác."""
        self.state = SystemState()
        self.settings = make_settings()
        self.relay = FakeRelay()
        self.controller = SystemController(self.state, self.settings, self.relay)

    def test_initial_state_is_off_and_outputs_are_disabled(self) -> None:
        """Với dữ liệu mặc định, khởi động OFF không được tự cấp đầu ra hay báo lỗi."""
        snapshot = self.controller.get_state_snapshot()

        self.assertEqual(snapshot.mode, ControlMode.OFF)
        self.assertFalse(snapshot.heater_on)
        self.assertFalse(snapshot.process_fan_on)
        self.assertEqual(snapshot.alarm_code, AlarmCode.NONE)

    def test_first_safe_cycle_synchronizes_real_outputs_to_off(self) -> None:
        """Khi relay còn bật, chu kỳ an toàn đầu tiên phải đồng bộ về OFF."""
        # Thử từng đầu ra còn bật mà không giả lập heater và quạt cùng hoạt động.
        # State mặc định OFF chưa đủ chứng minh phần cứng thực tế đã tắt.
        for heater_on, process_fan_on in ((True, False), (False, True)):
            with self.subTest(heater_on=heater_on, process_fan_on=process_fan_on):
                relay = FakeRelay(heater_on=heater_on, process_fan_on=process_fan_on)
                controller = SystemController(SystemState(), make_settings(), relay)
                controller.update_measurement(25.0, True, True)

                controller.run_control_cycle()

                snapshot = controller.get_state_snapshot()
                self.assertIn(("all_outputs_off", None), relay.command_history)
                self.assertFalse(relay.heater_on)
                self.assertFalse(relay.process_fan_on)
                self.assertFalse(snapshot.heater_on)
                self.assertFalse(snapshot.process_fan_on)
                self.assertEqual(snapshot.mode, ControlMode.OFF)
                self.assertEqual(snapshot.alarm_code, AlarmCode.NONE)
                self.assertIsNone(controller.get_active_alarm())

    def test_off_mode_accepts_setpoint_without_starting_outputs(self) -> None:
        """Ở 20°C, lưu setpoint 30°C trong OFF không được tự khởi động tải."""
        self.controller.update_measurement(20.0, True, True)
        self.controller.run_control_cycle()
        command = ControlCommand(
            command_type=CommandType.SET_TEMPERATURE,
            source=CommandSource.LOCAL_HMI,
            value=30.0,
        )

        result = self.controller.process_command(command)
        self.controller.run_control_cycle()

        # OFF phải ngăn tự sưởi dù phép đo thấp hơn điểm đặt.
        snapshot = self.controller.get_state_snapshot()
        self.assertTrue(result.accepted)
        self.assertEqual(
            self.controller.get_settings_snapshot().temperature_setpoint_c, 30.0
        )
        self.assertEqual(snapshot.mode, ControlMode.OFF)
        self.assertFalse(snapshot.heater_on)
        self.assertFalse(snapshot.process_fan_on)
        self.assertFalse(self.relay.heater_on)
        self.assertFalse(self.relay.process_fan_on)
        self.assertNotIn(("set_heater", True), self.relay.command_history)
        self.assertNotIn(("set_process_fan", True), self.relay.command_history)

    def test_off_mode_rejects_direct_heater_command(self) -> None:
        """Kết nối an toàn vẫn không cho HMI bật sưởi trực tiếp khi đang OFF."""
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()

        result = self.controller.process_command(
            ControlCommand(
                command_type=CommandType.SET_HEATER,
                source=CommandSource.LOCAL_HMI,
                value=True,
            )
        )

        # Từ chối do mode OFF, không dựa vào lỗi kết nối để chặn tải.
        snapshot = self.controller.get_state_snapshot()
        self.assertFalse(result.accepted)
        self.assertFalse(snapshot.heater_on)
        self.assertFalse(snapshot.process_fan_on)
        self.assertFalse(self.relay.heater_on)
        self.assertFalse(self.relay.process_fan_on)
        self.assertEqual(snapshot.mode, ControlMode.OFF)
        self.assertNotIn(("set_heater", True), self.relay.command_history)

    def test_off_mode_rejects_direct_fan_command(self) -> None:
        """Kết nối an toàn vẫn không cho HMI bật quạt trực tiếp khi đang OFF."""
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()

        result = self.controller.process_command(
            ControlCommand(
                command_type=CommandType.SET_PROCESS_FAN,
                source=CommandSource.LOCAL_HMI,
                value=True,
            )
        )

        # Chế độ OFF phải chặn cả quạt, tránh vận hành tải ngoài ý muốn.
        snapshot = self.controller.get_state_snapshot()
        self.assertFalse(result.accepted)
        self.assertFalse(snapshot.heater_on)
        self.assertFalse(snapshot.process_fan_on)
        self.assertFalse(self.relay.heater_on)
        self.assertFalse(self.relay.process_fan_on)
        self.assertEqual(snapshot.mode, ControlMode.OFF)
        self.assertNotIn(("set_process_fan", True), self.relay.command_history)

    def test_off_mode_continues_monitoring_temperature(self) -> None:
        """Mẫu đo 25°C rồi 26,5°C phải được cập nhật trong OFF mà không bật tải."""
        # OFF chỉ dừng đầu ra, không dừng giám sát nhiệt độ và kết nối.
        for temperature_c in (25.0, 26.5):
            self.controller.update_measurement(temperature_c, True, True)
            self.controller.run_control_cycle()

            snapshot = self.controller.get_state_snapshot()
            self.assertEqual(snapshot.temperature_c, temperature_c)
            self.assertEqual(snapshot.mode, ControlMode.OFF)
            self.assertFalse(snapshot.heater_on)
            self.assertFalse(snapshot.process_fan_on)
            self.assertTrue(snapshot.sensor_connected)
            self.assertTrue(snapshot.modbus_connected)
            self.assertFalse(self.relay.heater_on)
            self.assertFalse(self.relay.process_fan_on)


class TestSystemControllerManualMode(unittest.TestCase):
    """Kiểm tra điều khiển thủ công và phân quyền khi kết nối an toàn."""

    def setUp(self) -> None:
        """Mỗi test bắt đầu OFF ở 25°C, đã kết nối và đồng bộ relay."""
        self.relay = FakeRelay()
        self.controller = SystemController(SystemState(), make_settings(), self.relay)
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()

    def _enter_manual(self) -> None:
        """Dùng lệnh HMI hợp lệ để thiết lập tiền điều kiện MANUAL."""
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_MODE,
                CommandSource.LOCAL_HMI,
                "MANUAL",
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.MANUAL)

    def _assert_outputs(self, heater_on: bool, process_fan_on: bool) -> None:
        """Đối chiếu snapshot với relay giả để tránh chỉ kiểm tra trạng thái phần mềm."""
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.heater_on, heater_on)
        self.assertEqual(snapshot.process_fan_on, process_fan_on)
        self.assertEqual(self.relay.heater_on, heater_on)
        self.assertEqual(self.relay.process_fan_on, process_fan_on)

    def _assert_history_matches_outputs(
        self,
        history: list[tuple[str, bool | None]],
        heater_on: bool,
        process_fan_on: bool,
    ) -> tuple[bool, bool]:
        """Dựng lại trạng thái sau mỗi lệnh; hai đầu ra có thể cùng ON."""
        for action, enabled in history:
            if action == "set_heater":
                heater_on = enabled
            elif action == "set_process_fan":
                process_fan_on = enabled
            elif action == "all_outputs_off":
                heater_on = process_fan_on = False
            else:
                self.fail(f"Lệnh relay không nhận diện được: {action}")
        return heater_on, process_fan_on

    def test_local_hmi_can_enter_manual_mode_safely(self) -> None:
        """Từ OFF an toàn, HMI được vào MANUAL nhưng không tự bật tải."""
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_MODE,
                CommandSource.LOCAL_HMI,
                "MANUAL",
            )
        )

        self.assertTrue(result.accepted)
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.mode, ControlMode.MANUAL)
        self.assertEqual(snapshot.alarm_code, AlarmCode.NONE)
        self.assertIsNone(self.controller.get_active_alarm())
        self._assert_outputs(False, False)

    def test_manual_mode_can_turn_heater_on_and_off(self) -> None:
        """Trong MANUAL, lệnh HMI bật/tắt sưởi được thực hiện và quạt luôn tắt."""
        self._enter_manual()

        for enabled in (True, False):
            result = self.controller.process_command(
                ControlCommand(
                    CommandType.SET_HEATER,
                    CommandSource.LOCAL_HMI,
                    enabled,
                )
            )
            self.assertTrue(result.accepted)
            self._assert_outputs(enabled, False)
            self.assertEqual(
                self.controller.get_state_snapshot().mode, ControlMode.MANUAL
            )

    def test_manual_mode_can_turn_fan_on_and_off(self) -> None:
        """Trong MANUAL, lệnh HMI bật/tắt quạt được thực hiện và sưởi luôn tắt."""
        self._enter_manual()

        for enabled in (True, False):
            result = self.controller.process_command(
                ControlCommand(
                    CommandType.SET_PROCESS_FAN,
                    CommandSource.LOCAL_HMI,
                    enabled,
                )
            )
            self.assertTrue(result.accepted)
            self._assert_outputs(False, enabled)
            self.assertEqual(
                self.controller.get_state_snapshot().mode, ControlMode.MANUAL
            )

    def test_manual_can_turn_fan_on_while_heater_is_running(self) -> None:
        """MANUAL cho phép bật quạt mà không tự tắt heater đang chạy."""
        self._enter_manual()
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_HEATER,
                CommandSource.LOCAL_HMI,
                True,
            )
        )
        self.assertTrue(result.accepted)
        self._assert_outputs(True, False)
        start = len(self.relay.command_history)

        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_PROCESS_FAN,
                CommandSource.LOCAL_HMI,
                True,
            )
        )

        self.assertTrue(result.accepted)
        self._assert_outputs(True, True)
        history = self.relay.command_history[start:]
        self.assertIn(("set_process_fan", True), history)
        self.assertNotIn(("set_heater", False), history)
        self.assertEqual(
            self._assert_history_matches_outputs(history, True, False),
            (True, True),
        )

    def test_manual_can_turn_heater_on_while_fan_is_running(self) -> None:
        """MANUAL cho phép bật heater mà không tự tắt quạt đang chạy."""
        self._enter_manual()
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_PROCESS_FAN,
                CommandSource.LOCAL_HMI,
                True,
            )
        )
        self.assertTrue(result.accepted)
        self._assert_outputs(False, True)
        start = len(self.relay.command_history)

        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_HEATER,
                CommandSource.LOCAL_HMI,
                True,
            )
        )

        self.assertTrue(result.accepted)
        self._assert_outputs(True, True)
        history = self.relay.command_history[start:]
        self.assertIn(("set_heater", True), history)
        self.assertNotIn(("set_process_fan", False), history)
        self.assertEqual(
            self._assert_history_matches_outputs(history, False, True),
            (True, True),
        )

    def test_manual_mode_does_not_use_setpoint_automatically(self) -> None:
        """Setpoint 30°C không tự bật sưởi ở 20°C hay tắt lệnh sưởi thủ công ở 35°C."""
        self._enter_manual()
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_TEMPERATURE,
                CommandSource.LOCAL_HMI,
                30.0,
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(
            self.controller.get_settings_snapshot().temperature_setpoint_c, 30.0
        )
        self.controller.update_measurement(20.0, True, True)
        self.controller.run_control_cycle()
        self._assert_outputs(False, False)

        # 35°C vẫn an toàn: MANUAL phải giữ quyết định của người vận hành.
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_HEATER,
                CommandSource.LOCAL_HMI,
                True,
            )
        )
        self.assertTrue(result.accepted)
        self.controller.update_measurement(35.0, True, True)
        self.controller.run_control_cycle()
        self._assert_outputs(True, False)
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.MANUAL)

    def test_remote_scada_cannot_enter_manual_mode(self) -> None:
        """Từ OFF an toàn, SCADA không được giành quyền điều khiển thủ công."""
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_MODE,
                CommandSource.REMOTE_SCADA,
                "MANUAL",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.OFF)
        self._assert_outputs(False, False)

    def test_remote_scada_cannot_control_outputs_in_manual_mode(self) -> None:
        """Dù HMI đã chọn MANUAL, SCADA vẫn không được bật trực tiếp hai tải."""
        self._enter_manual()

        for command_type in (CommandType.SET_HEATER, CommandType.SET_PROCESS_FAN):
            with self.subTest(command_type=command_type):
                result = self.controller.process_command(
                    ControlCommand(
                        command_type,
                        CommandSource.REMOTE_SCADA,
                        True,
                    )
                )
                self.assertFalse(result.accepted)
                self._assert_outputs(False, False)
                self.assertEqual(
                    self.controller.get_state_snapshot().mode, ControlMode.MANUAL
                )


class TestSystemControllerAutoMode(unittest.TestCase):
    """Kiểm tra trình tự AUTO bằng đồng hồ giả, không cần chờ thời gian thật."""

    def setUp(self) -> None:
        """Tạo hệ thống OFF sạch, cấu hình chu kỳ AUTO ngắn để kiểm thử."""
        self.now = 100.0
        self._create_controller()

    def _create_controller(self) -> None:
        """Đồng bộ OFF và đặt setpoint 30°C trước mỗi kịch bản độc lập."""
        self.relay = FakeRelay()
        self.controller = SystemController(
            SystemState(),
            make_settings(control_deadband_c=2.0),
            self.relay,
            clock=lambda: self.now,
        )
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_TEMPERATURE,
                CommandSource.LOCAL_HMI,
                30.0,
            )
        )
        self.assertTrue(result.accepted)

    def _enter_auto(self, source: CommandSource = CommandSource.LOCAL_HMI) -> None:
        """Chuyển AUTO qua lệnh công khai, chưa chạy chu kỳ điều khiển."""
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_MODE,
                source,
                "AUTO",
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.AUTO)
        self._assert_outputs(False, False)

    def _assert_outputs(self, heater_on: bool, process_fan_on: bool) -> None:
        """Đối chiếu trạng thái controller với relay giả."""
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(
            (snapshot.heater_on, snapshot.process_fan_on), (heater_on, process_fan_on)
        )
        self.assertEqual(
            (self.relay.heater_on, self.relay.process_fan_on),
            (heater_on, process_fan_on),
        )

    def _advance(self, seconds: float) -> None:
        """Tiến đồng hồ giả và chạy thêm một chu kỳ điều khiển."""
        self.now += seconds
        self.controller.run_control_cycle()

    def _cycle_at(
        self, temperature_c: float, heater_on: bool, process_fan_on: bool
    ) -> None:
        """Chạy một mẫu đo an toàn và xác minh đầu ra mong đợi trong AUTO."""
        self.controller.update_measurement(temperature_c, True, True)
        self.controller.run_control_cycle()
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.AUTO)
        self._assert_outputs(heater_on, process_fan_on)

    def test_local_hmi_can_enter_auto_mode_safely(self) -> None:
        """HMI vào AUTO ở 25°C nhưng chưa bật sưởi trước chu kỳ tiếp theo."""
        self._enter_auto()
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.alarm_code, AlarmCode.NONE)
        self.assertIsNone(self.controller.get_active_alarm())

    def test_controller_restart_does_not_resume_auto_mode(self) -> None:
        """Controller tạo lại từ state AUTO phải khởi đầu OFF và đồng bộ relay."""
        relay = FakeRelay(heater_on=True, process_fan_on=True)
        controller = SystemController(
            SystemState(mode=ControlMode.AUTO),
            make_settings(),
            relay,
            clock=lambda: self.now,
        )
        self.assertEqual(controller.get_state_snapshot().mode, ControlMode.OFF)

        controller.update_measurement(25.0, True, True)
        controller.run_control_cycle()

        self.assertEqual(controller.get_state_snapshot().mode, ControlMode.OFF)
        self.assertFalse(relay.heater_on)
        self.assertFalse(relay.process_fan_on)

    def test_auto_timing_settings_reject_invalid_values(self) -> None:
        """Các khoảng thời gian không nhận bool, số âm hoặc số không hữu hạn."""
        for value in (-1.0, float("nan"), float("inf"), True):
            with self.subTest(value=value):
                for setting_name in (
                    "heater_to_fan_start_delay_s",
                    "drying_duration_s",
                    "fan_start_confirmation_timeout_s",
                ):
                    with self.subTest(setting_name=setting_name):
                        with self.assertRaises(ValueError):
                            SystemController(
                                SystemState(),
                                make_settings(**{setting_name: value}),
                                FakeRelay(),
                            )

    def test_fan_start_confirmation_timeout_triggers_heater_off_and_retry(self) -> None:
        """Xác nhận quá 10 giây thì tắt heater và ghi/readback quạt lại đúng một lần."""
        now = [100.0]

        class SlowFirstFanRelay(FakeRelay):
            slow_next_fan_start = True

            def set_process_fan(self, enabled: bool) -> None:
                if enabled and self.slow_next_fan_start:
                    self.slow_next_fan_start = False
                    now[0] += 10.1
                super().set_process_fan(enabled)

        relay = SlowFirstFanRelay()
        controller = SystemController(
            SystemState(),
            make_settings(temperature_setpoint_c=30.0),
            relay,
            clock=lambda: now[0],
        )
        controller.update_measurement(25.0, True, True)
        controller.run_control_cycle()
        self.assertTrue(
            controller.process_command(
                ControlCommand(
                    CommandType.SET_MODE,
                    CommandSource.LOCAL_HMI,
                    "AUTO",
                )
            ).accepted
        )
        controller.update_measurement(28.0, True, True)
        controller.run_control_cycle()
        now[0] += 5.0

        controller.run_control_cycle()

        snapshot = controller.get_state_snapshot()
        self.assertEqual(snapshot.mode, ControlMode.AUTO)
        self.assertEqual(snapshot.auto_phase, AutoPhase.HEATING)
        self.assertFalse(snapshot.heater_on)
        self.assertTrue(snapshot.process_fan_on)
        self.assertEqual(relay.command_history.count(("set_process_fan", True)), 2)

    def test_remote_scada_can_enter_auto_and_return_to_off(self) -> None:
        """SCADA được chọn AUTO rồi OFF, kết thúc với cả hai đầu ra tắt."""
        self._enter_auto(CommandSource.REMOTE_SCADA)
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_MODE,
                CommandSource.REMOTE_SCADA,
                "OFF",
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.OFF)
        self._assert_outputs(False, False)

    def test_auto_heating_hysteresis(self) -> None:
        """Gia nhiệt đến S; giữ quạt liên tục và đóng/ngắt heater trong HOLDING."""
        self._enter_auto()
        self._cycle_at(29.0, True, False)
        self._advance(4.9)
        self._assert_outputs(True, False)
        self._advance(0.1)
        self._assert_outputs(True, True)
        self._cycle_at(30.0, False, True)
        self.assertEqual(
            self.controller.get_state_snapshot().auto_phase, AutoPhase.HOLDING
        )
        self._cycle_at(29.0, False, True)
        self._cycle_at(28.0, True, True)
        self._cycle_at(29.0, True, True)
        self._cycle_at(30.0, False, True)
        self._advance(10)
        self._assert_outputs(False, True)

    def test_auto_starts_above_lower_threshold_without_waiting_forever(self) -> None:
        """AUTO dưới setpoint vẫn gia nhiệt dù chưa xuống tới S-D."""
        self._enter_auto()
        self._cycle_at(29.0, True, False)

    def test_auto_rejects_direct_output_commands(self) -> None:
        """AUTO phải từ chối HMI bật tải trực tiếp để giữ quyền điều khiển tự động."""
        self._enter_auto()
        for command_type in (CommandType.SET_HEATER, CommandType.SET_PROCESS_FAN):
            with self.subTest(command_type=command_type):
                result = self.controller.process_command(
                    ControlCommand(
                        command_type,
                        CommandSource.LOCAL_HMI,
                        True,
                    )
                )
                self.assertFalse(result.accepted)
                self.assertEqual(
                    self.controller.get_state_snapshot().mode, ControlMode.AUTO
                )
                self._assert_outputs(False, False)

    def test_auto_rejects_recipe_change_during_batch(self) -> None:
        """Không đổi setpoint giữa mẻ làm sai thời gian đủ điều kiện."""
        self._enter_auto()
        self._cycle_at(25.0, True, False)
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_TEMPERATURE,
                CommandSource.LOCAL_HMI,
                24.0,
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(
            self.controller.get_settings_snapshot().temperature_setpoint_c, 30.0
        )
        self._assert_outputs(True, False)

    def test_leaving_auto_for_off_disables_active_output(self) -> None:
        """Rời AUTO trong preheat hủy timer và tắt cả hai đầu ra."""
        self._enter_auto()
        self._cycle_at(28.0, True, False)
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_MODE,
                CommandSource.LOCAL_HMI,
                "OFF",
            )
        )
        self.assertTrue(result.accepted)
        self._assert_outputs(False, False)
        self._advance(10.0)
        self._assert_outputs(False, False)

    def test_temperature_reaches_setpoint_before_fan_start_delay(self) -> None:
        """Đạt setpoint sớm thì bật quạt và bắt đầu HOLDING ngay."""
        self._enter_auto()
        self._cycle_at(28.0, True, False)
        self._cycle_at(30.0, False, True)
        self.assertEqual(
            self.controller.get_state_snapshot().auto_phase, AutoPhase.HOLDING
        )
        self.assertEqual(self.controller.get_state_snapshot().drying_elapsed_s, 0)

    def test_failed_fan_start_turns_heater_off_and_retries_once(self) -> None:
        """Retry xác nhận được quạt; chỉ cho heater bật lại ở chu kỳ an toàn kế."""
        self._enter_auto()
        self._cycle_at(28.0, True, False)
        self.relay.fail_next_operations["set_process_fan"] = 1
        self._advance(5.0)
        self._assert_outputs(False, True)
        self.assertEqual(self.relay.command_history.count(("set_process_fan", True)), 2)
        self.assertEqual(
            self.controller.get_state_snapshot().auto_phase, AutoPhase.HEATING
        )
        self._advance(0.2)
        self._assert_outputs(True, True)

    def test_failed_fan_retry_latches_fault_and_outputs_off(self) -> None:
        """Hai lần bật quạt lỗi làm phát sinh FAULT và cố gắng tắt mọi đầu ra."""
        self._enter_auto()
        self._cycle_at(28.0, True, False)
        self.relay.fail_operations.add("set_process_fan")
        self._advance(5.0)

        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.mode, ControlMode.FAULT)
        self.assertEqual(snapshot.alarm_code, AlarmCode.PROCESS_FAN_START_FAILED)
        alarm = self.controller.get_active_alarm()
        self.assertIsNotNone(alarm)
        assert alarm is not None
        self.assertIn("Không xác nhận được process_fan ON", alarm.message)
        self.assertFalse(self.relay.heater_on)
        self.assertFalse(self.relay.process_fan_on)
        self.assertEqual(self.relay.command_history.count(("set_process_fan", True)), 2)

    def test_holding_keeps_fan_when_temperature_drops(self) -> None:
        """HOLDING bật lại heater nhưng không tắt quạt hoặc reset thời gian."""
        self._enter_auto()
        self._cycle_at(28.0, True, False)
        self._advance(5.0)
        self._cycle_at(30.0, False, True)
        self._cycle_at(28.0, True, True)
        self.assertEqual(
            self.controller.get_state_snapshot().auto_phase, AutoPhase.HOLDING
        )

    def test_reconnect_forces_off_once_without_restarting_fault(self) -> None:
        """Reconnect đồng bộ OFF một lần; phép đo kế tiếp không ép OFF lần nữa."""
        self._enter_auto()
        self._cycle_at(28.0, True, False)
        off_count_before_disconnect = self.relay.command_history.count(
            ("all_outputs_off", None)
        )

        self.controller.update_measurement(28.0, True, False)
        self.controller.run_control_cycle()
        self.controller.update_measurement(28.0, True, True)
        self.controller.run_control_cycle()
        count_after_reconnect = self.relay.command_history.count(
            ("all_outputs_off", None)
        )
        self.assertEqual(count_after_reconnect, off_count_before_disconnect + 1)
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.FAULT)

        self.controller.update_measurement(28.0, True, True)
        self.controller.run_control_cycle()
        self.assertEqual(
            self.relay.command_history.count(("all_outputs_off", None)),
            count_after_reconnect,
        )


class TestSystemControllerFaultMode(unittest.TestCase):
    """Kiểm tra dừng an toàn, giữ FAULT và quyền reset qua public API."""

    def setUp(self) -> None:
        """Mỗi test có hệ thống riêng, kết nối an toàn và đầu ra đã đồng bộ OFF."""
        self._create_controller()

    def _create_controller(self) -> None:
        """Tạo mới cả controller và relay để FAULT không lan giữa các subtest."""
        self.relay = FakeRelay()
        self.settings = make_settings()
        self.controller = SystemController(SystemState(), self.settings, self.relay)
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()

    def _start_manual_heating(self) -> None:
        """Bật sưởi qua HMI để kiểm tra lỗi thực sự ngắt một tải đang chạy."""
        for kind, value in (
            (CommandType.SET_MODE, "MANUAL"),
            (CommandType.SET_HEATER, True),
        ):
            result = self.controller.process_command(
                ControlCommand(
                    kind,
                    CommandSource.LOCAL_HMI,
                    value,
                )
            )
            self.assertTrue(result.accepted)
        self._assert_outputs(True, False)

    def _assert_outputs(self, heater_on: bool, process_fan_on: bool) -> None:
        """Đối chiếu phần mềm với relay giả khi Modbus còn hoạt động."""
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(
            (snapshot.heater_on, snapshot.process_fan_on), (heater_on, process_fan_on)
        )
        self.assertEqual(
            (self.relay.heater_on, self.relay.process_fan_on),
            (heater_on, process_fan_on),
        )

    def _assert_fault(self, code: AlarmCode) -> None:
        """FAULT phải có mã lỗi và cảnh báo active, không chỉ đổi mode."""
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.mode, ControlMode.FAULT)
        self.assertEqual(snapshot.alarm_code, code)
        alarm = self.controller.get_active_alarm()
        self.assertIsNotNone(alarm)
        self.assertEqual(alarm.code, code)
        self.assertTrue(alarm.active)

    def _overheat(self) -> None:
        """Gây lỗi đúng tại giới hạn quá nhiệt để thiết lập tiền điều kiện reset."""
        self.controller.update_measurement(
            self.settings.high_temperature_limit_c, True, True
        )
        self.controller.run_control_cycle()
        self._assert_fault(AlarmCode.OVER_TEMPERATURE)

    def test_local_emergency_stop_enters_fault_and_disables_outputs(self) -> None:
        """Dừng khẩn tại HMI phải ngắt sưởi đang chạy và giữ cảnh báo FAULT."""
        self._start_manual_heating()
        result = self.controller.process_command(
            ControlCommand(
                CommandType.EMERGENCY_STOP,
                CommandSource.LOCAL_HMI,
            )
        )
        self.assertTrue(result.accepted)
        self._assert_outputs(False, False)
        self._assert_fault(AlarmCode.EMERGENCY_STOPPED)

    def test_expired_remote_emergency_stop_is_still_processed(self) -> None:
        """Lệnh dừng khẩn remote cũ 10 giây vẫn phải được xử lý để ngắt tải."""
        self._start_manual_heating()
        command = ControlCommand(CommandType.EMERGENCY_STOP, CommandSource.REMOTE_SCADA)
        command.timestamp_ms -= 10_000
        result = self.controller.process_command(command)
        self.assertTrue(result.accepted)
        self._assert_outputs(False, False)
        self._assert_fault(AlarmCode.EMERGENCY_STOPPED)

    def test_sensor_disconnect_enters_fault_and_requests_cooling(self) -> None:
        """Mất cảm biến khi sưởi chạy phải tắt sưởi, bật quạt nếu bus còn tốt."""
        self._start_manual_heating()
        self.controller.update_measurement(None, False, True)
        self.controller.run_control_cycle()
        self._assert_outputs(False, True)
        self._assert_fault(AlarmCode.SENSOR_DISCONNECTED)

    def test_invalid_temperature_values_enter_sensor_fault(self) -> None:
        """Cảm biến báo connected nhưng số đo thiếu/không hữu hạn vẫn phải khóa sưởi."""
        for temperature in (None, float("nan"), float("inf"), float("-inf")):
            with self.subTest(temperature=temperature):
                self._create_controller()
                self.controller.update_measurement(temperature, True, True)
                self.controller.run_control_cycle()
                self._assert_fault(AlarmCode.SENSOR_DISCONNECTED)
                self.assertFalse(self.controller.get_state_snapshot().heater_on)
                self.assertFalse(self.relay.heater_on)
                self.assertNotIn(("set_heater", True), self.relay.command_history)

    def test_modbus_disconnect_enters_fault_without_crashing(self) -> None:
        """Mất bus khi đang sưởi phải báo FAULT, không làm chu kỳ ném exception."""
        self._start_manual_heating()
        self.controller.update_measurement(25.0, True, False)
        # Không suy diễn relay vật lý đã OFF khi mất đường truyền xác nhận.
        self.controller.run_control_cycle()
        self._assert_fault(AlarmCode.MODBUS_COMMUNICATION_LOST)
        self.assertFalse(self.controller.get_state_snapshot().modbus_connected)

    def test_overtemperature_enters_fault_and_requests_cooling(self) -> None:
        """Cả đúng ngưỡng và vượt ngưỡng quá nhiệt đều phải tắt sưởi, bật quạt."""
        for offset in (0.0, 1.0):
            with self.subTest(offset=offset):
                self._create_controller()
                self._start_manual_heating()
                self.controller.update_measurement(
                    self.settings.high_temperature_limit_c + offset,
                    True,
                    True,
                )
                self.controller.run_control_cycle()
                self._assert_outputs(False, True)
                self._assert_fault(AlarmCode.OVER_TEMPERATURE)

    def test_mqtt_disconnect_does_not_enter_fault(self) -> None:
        """Mất MQTT không cản AUTO; khi heater dừng thì quạt chạy post-run."""
        self.controller.update_mqtt_connection(True)
        for kind, value in (
            (CommandType.SET_TEMPERATURE, 30.0),
            (CommandType.SET_MODE, "AUTO"),
        ):
            result = self.controller.process_command(
                ControlCommand(
                    kind,
                    CommandSource.LOCAL_HMI,
                    value,
                )
            )
            self.assertTrue(result.accepted)
        self.controller.update_mqtt_connection(False)
        self.controller.run_control_cycle()
        self._assert_outputs(True, False)
        self.controller.update_measurement(30.0, True, True)
        self.controller.run_control_cycle()
        self._assert_outputs(False, True)
        snapshot = self.controller.get_state_snapshot()
        self.assertFalse(snapshot.mqtt_connected)
        self.assertEqual(snapshot.mode, ControlMode.AUTO)
        self.assertEqual(snapshot.alarm_code, AlarmCode.NONE)
        self.assertIsNone(self.controller.get_active_alarm())

    def test_fault_remains_latched_after_original_condition_clears(self) -> None:
        """Nhiệt độ trở lại an toàn không được tự xóa FAULT trước khi HMI reset."""
        self._overheat()
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()
        # Giữ lỗi để người vận hành xác nhận, tránh hệ thống tự khởi động lại.
        self._assert_fault(AlarmCode.OVER_TEMPERATURE)

    def test_remote_scada_cannot_reset_fault(self) -> None:
        """Dù điều kiện đã an toàn, SCADA không có quyền xóa lỗi thay HMI."""
        self._overheat()
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()
        result = self.controller.process_command(
            ControlCommand(
                CommandType.RESET_ALARM,
                CommandSource.REMOTE_SCADA,
            )
        )
        self.assertFalse(result.accepted)
        self._assert_fault(AlarmCode.OVER_TEMPERATURE)

    def test_local_reset_is_rejected_while_fault_condition_remains(self) -> None:
        """HMI không được reset khi cảm biến/bus lỗi hoặc chưa hạ tới ngưỡng reset."""
        for condition in ("sensor", "modbus", "temperature"):
            with self.subTest(condition=condition):
                self._create_controller()
                if condition == "sensor":
                    self.controller.update_measurement(None, False, True)
                    code = AlarmCode.SENSOR_DISCONNECTED
                elif condition == "modbus":
                    self.controller.update_measurement(25.0, True, False)
                    code = AlarmCode.MODBUS_COMMUNICATION_LOST
                else:
                    self._overheat()
                    # Đã dưới ngưỡng quá nhiệt nhưng chưa đủ thấp để reset an toàn.
                    reset_limit = (
                        self.settings.high_temperature_limit_c
                        - self.settings.control_deadband_c
                    )
                    self.controller.update_measurement(reset_limit + 0.5, True, True)
                    code = AlarmCode.OVER_TEMPERATURE
                self.controller.run_control_cycle()
                self._assert_fault(code)
                result = self.controller.process_command(
                    ControlCommand(
                        CommandType.RESET_ALARM,
                        CommandSource.LOCAL_HMI,
                    )
                )
                self.assertFalse(result.accepted)
                self._assert_fault(code)

    def test_successful_local_reset_returns_system_to_off(self) -> None:
        """Reset tại đúng ngưỡng an toàn phải xóa alarm và về OFF, không tự chạy tải."""
        self._overheat()
        reset_limit = (
            self.settings.high_temperature_limit_c - self.settings.control_deadband_c
        )
        self.controller.update_measurement(reset_limit, True, True)
        result = self.controller.process_command(
            ControlCommand(
                CommandType.RESET_ALARM,
                CommandSource.LOCAL_HMI,
            )
        )
        self.assertTrue(result.accepted)
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.mode, ControlMode.OFF)
        self.assertEqual(snapshot.alarm_code, AlarmCode.NONE)
        self._assert_outputs(False, False)
        self.assertIsNone(self.controller.get_active_alarm())


class TestSystemControllerRobustness(unittest.TestCase):
    """Kiểm tra dữ liệu lệnh, lỗi relay và tính độc lập của snapshot."""

    def setUp(self) -> None:
        """Tạo hệ thống riêng ở OFF với kết nối hợp lệ."""
        self._create_controller()

    def _create_controller(self) -> None:
        """Khởi tạo mới cho từng subtest để kết quả không phụ thuộc trường hợp trước."""
        self.relay = FakeRelay()
        self.controller = SystemController(SystemState(), make_settings(), self.relay)
        self.controller.update_measurement(25.0, True, True)
        self.controller.run_control_cycle()

    def _enter_manual(self) -> None:
        """Chuyển MANUAL bằng public API để kiểm tra lệnh đầu ra đúng bối cảnh."""
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_MODE,
                CommandSource.LOCAL_HMI,
                "MANUAL",
            )
        )
        self.assertTrue(result.accepted)

    def _assert_no_fault(self, mode: ControlMode = ControlMode.OFF) -> None:
        """Lệnh sai phải bị từ chối riêng, không gây FAULT cho toàn hệ thống."""
        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.mode, mode)
        self.assertEqual(snapshot.alarm_code, AlarmCode.NONE)
        self.assertIsNone(self.controller.get_active_alarm())

    def test_expired_remote_non_emergency_command_is_rejected(self) -> None:
        """Setpoint từ SCADA cũ 10 giây phải bị từ chối mà không tác động hệ thống."""
        settings = self.controller.get_settings_snapshot()
        history = list(self.relay.command_history)
        command = ControlCommand(
            CommandType.SET_TEMPERATURE, CommandSource.REMOTE_SCADA, 35.0
        )
        command.timestamp_ms -= 10_000

        result = self.controller.process_command(command)

        self.assertFalse(result.accepted)
        self.assertEqual(self.controller.get_settings_snapshot(), settings)
        self._assert_no_fault()
        self.assertEqual(self.relay.command_history, history)
        snapshot = self.controller.get_state_snapshot()
        self.assertFalse(snapshot.heater_on)
        self.assertFalse(snapshot.process_fan_on)
        self.assertFalse(self.relay.heater_on)
        self.assertFalse(self.relay.process_fan_on)

    def test_invalid_setpoint_types_are_rejected(self) -> None:
        """Bool, chuỗi, None và số không hữu hạn không được thay thế setpoint."""
        for value in (True, "30", None, float("nan"), float("inf")):
            with self.subTest(value=value):
                self._create_controller()
                settings = self.controller.get_settings_snapshot()
                result = self.controller.process_command(
                    ControlCommand(
                        CommandType.SET_TEMPERATURE,
                        CommandSource.LOCAL_HMI,
                        value,
                    )
                )
                self.assertFalse(result.accepted)
                self.assertEqual(self.controller.get_settings_snapshot(), settings)
                self._assert_no_fault()

    def test_out_of_range_setpoints_are_rejected(self) -> None:
        """Giới hạn setpoint bao gồm hai biên; giá trị ngoài biên bị từ chối."""
        settings = self.controller.get_settings_snapshot()
        cases = (
            (settings.minimum_setpoint_c - 1.0, False),
            (settings.maximum_setpoint_c + 1.0, False),
            (settings.minimum_setpoint_c, True),
            (settings.maximum_setpoint_c, True),
        )
        for value, accepted in cases:
            with self.subTest(value=value):
                self._create_controller()
                previous = (
                    self.controller.get_settings_snapshot().temperature_setpoint_c
                )
                result = self.controller.process_command(
                    ControlCommand(
                        CommandType.SET_TEMPERATURE,
                        CommandSource.LOCAL_HMI,
                        value,
                    )
                )
                self.assertEqual(result.accepted, accepted)
                self.assertEqual(
                    self.controller.get_settings_snapshot().temperature_setpoint_c,
                    value if accepted else previous,
                )
                self._assert_no_fault()

    def test_output_commands_require_strict_boolean_values(self) -> None:
        """Trong MANUAL, số 0/1, chuỗi và None không được hiểu ngầm là bool."""
        for kind, values in (
            (CommandType.SET_HEATER, (1, 0, "true", None)),
            (CommandType.SET_PROCESS_FAN, (1, 0, "false", None)),
        ):
            for value in values:
                with self.subTest(kind=kind, value=value):
                    self._create_controller()
                    self._enter_manual()
                    history = list(self.relay.command_history)
                    result = self.controller.process_command(
                        ControlCommand(
                            kind,
                            CommandSource.LOCAL_HMI,
                            value,
                        )
                    )
                    self.assertFalse(result.accepted)
                    self._assert_no_fault(ControlMode.MANUAL)
                    snapshot = self.controller.get_state_snapshot()
                    self.assertFalse(snapshot.heater_on)
                    self.assertFalse(snapshot.process_fan_on)
                    self.assertFalse(self.relay.heater_on)
                    self.assertFalse(self.relay.process_fan_on)
                    self.assertEqual(self.relay.command_history, history)

    def test_invalid_mode_values_are_rejected(self) -> None:
        """Mode sai hoặc yêu cầu FAULT trực tiếp không được thay đổi mode hiện tại."""
        for value in ("UNKNOWN", "", 123, ControlMode.FAULT, "FAULT"):
            with self.subTest(value=value):
                self._create_controller()
                result = self.controller.process_command(
                    ControlCommand(
                        CommandType.SET_MODE,
                        CommandSource.LOCAL_HMI,
                        value,
                    )
                )
                self.assertFalse(result.accepted)
                self._assert_no_fault()

    def test_relay_heater_write_exception_enters_fault_without_crashing(self) -> None:
        """Ghi sưởi thất bại phải trả thất bại, vào FAULT và không xác nhận ON."""
        self._enter_manual()
        self.relay.fail_operations.add("set_heater")
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_HEATER,
                CommandSource.LOCAL_HMI,
                True,
            )
        )
        self.assertFalse(result.accepted)
        snapshot = self.controller.get_state_snapshot()
        self.assertFalse(snapshot.heater_on)
        self.assertFalse(self.relay.heater_on)
        self.assertEqual(snapshot.mode, ControlMode.FAULT)
        self.assertEqual(snapshot.alarm_code, AlarmCode.HEATER_CONTROL_FAILED)
        self.assertIn(("set_heater", True), self.relay.command_history)

    def test_relay_all_outputs_off_exception_enters_fault_without_crashing(
        self,
    ) -> None:
        """Đồng bộ lỗi khi kết nối lại không được ghi nhận đầu ra đã tắt thành công."""
        self._enter_manual()
        result = self.controller.process_command(
            ControlCommand(
                CommandType.SET_HEATER,
                CommandSource.LOCAL_HMI,
                True,
            )
        )
        self.assertTrue(result.accepted)
        self.assertTrue(self.controller.get_state_snapshot().heater_on)
        self.assertTrue(self.relay.heater_on)
        self.controller.update_measurement(25.0, True, False)
        self.controller.run_control_cycle()
        self.relay.fail_operations.add("all_outputs_off")
        self.controller.update_measurement(25.0, True, True)
        start = len(self.relay.command_history)

        self.controller.run_control_cycle()

        snapshot = self.controller.get_state_snapshot()
        self.assertEqual(snapshot.mode, ControlMode.FAULT)
        self.assertEqual(snapshot.alarm_code, AlarmCode.HEATER_CONTROL_FAILED)
        self.assertIn(("all_outputs_off", None), self.relay.command_history[start:])
        # Giữ lần xác nhận cuối khi OFF thất bại, không báo giả đồng bộ thành công.
        self.assertTrue(snapshot.heater_on)
        self.assertTrue(self.relay.heater_on)
        start = len(self.relay.command_history)
        self.controller.run_control_cycle()
        self.assertIn(("all_outputs_off", None), self.relay.command_history[start:])
        self.assertTrue(self.controller.get_state_snapshot().heater_on)

    def test_state_and_settings_snapshots_are_deep_copies(self) -> None:
        """Sửa snapshot bên ngoài không được thay đổi dữ liệu controller đang giữ."""
        original_state = self.controller.get_state_snapshot()
        original_settings = self.controller.get_settings_snapshot()
        state = self.controller.get_state_snapshot()
        settings = self.controller.get_settings_snapshot()
        self.assertIsNot(state, original_state)
        self.assertIsNot(settings, original_settings)
        state.temperature_c = -999.0
        state.heater_on = True
        state.mode = ControlMode.FAULT
        settings.temperature_setpoint_c = 999.0
        settings.control_deadband_c = 999.0

        new_state = self.controller.get_state_snapshot()
        new_settings = self.controller.get_settings_snapshot()
        self.assertEqual(new_state, original_state)
        self.assertEqual(new_settings, original_settings)
        self.assertIsNot(new_state, state)
        self.assertIsNot(new_settings, settings)

    def test_active_alarm_snapshot_is_a_deep_copy(self) -> None:
        """Sửa cảnh báo được trả ra không được xóa hoặc thay đổi lỗi nội bộ."""
        self.controller.update_measurement(None, False, True)
        self.controller.run_control_cycle()
        original = self.controller.get_active_alarm()
        alarm = self.controller.get_active_alarm()
        self.assertIsNotNone(original)
        self.assertIsNotNone(alarm)
        self.assertTrue(original.active)
        self.assertIsNot(alarm, original)
        alarm.active = False
        alarm.message = "Nội dung đã sửa bên ngoài"
        alarm.code = AlarmCode.NONE
        alarm.alarm_id = "changed"
        alarm.cleared_at_ms = 1

        new_alarm = self.controller.get_active_alarm()
        self.assertEqual(new_alarm, original)
        self.assertIsNot(new_alarm, alarm)
        self.assertEqual(
            self.controller.get_state_snapshot().alarm_code,
            AlarmCode.SENSOR_DISCONNECTED,
        )
