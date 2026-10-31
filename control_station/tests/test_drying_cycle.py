"""Chu trình sấy đủ thời gian trong vùng nhiệt, dùng đồng hồ/relay giả."""

import unittest

from control_station.controller import SystemController
from control_station.hmi.app import create_app
from control_station.models import (
    AlarmCode,
    AutoPhase,
    CommandSource,
    CommandType,
    ControlCommand,
    ControlMode,
    SystemState,
)
from control_station.tests.helpers import make_settings
from control_station.tests.test_controller import FakeRelay


class DryingCycleTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.relay = FakeRelay()
        self.settings = make_settings(
            temperature_setpoint_c=50,
            cooling_stop_temperature_c=35,
            drying_duration_s=10,
        )
        self.controller = SystemController(
            SystemState(),
            self.settings,
            self.relay,
            clock=lambda: self.now,
        )
        self.client = create_app(self.controller).test_client()
        self.tick(0, 28)

    def command(self, kind, value=None):
        return self.client.post(
            "/api/commands", json={"command_type": kind, "value": value}
        )

    def tick(self, seconds, temperature):
        self.now += seconds
        self.controller.update_measurement(temperature, True, True)
        self.controller.run_control_cycle()
        return self.controller.get_state_snapshot()

    def start_hold(self):
        self.assertEqual(self.command("START_DRYING").status_code, 200)
        state = self.tick(0, 50)
        self.assertEqual(state.auto_phase, AutoPhase.HOLDING)
        self.assertTrue(state.process_fan_on)
        return state

    def assert_outputs(self, heater, fan):
        state = self.controller.get_state_snapshot()
        self.assertEqual((state.heater_on, state.process_fan_on), (heater, fan))
        self.assertEqual(
            (self.relay.heater_on, self.relay.process_fan_on), (heater, fan)
        )

    def test_heating_time_does_not_count_toward_drying(self):
        self.command("START_DRYING")
        self.tick(0, 28)
        state = self.tick(4.9, 40)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assert_outputs(True, False)
        self.tick(0.1, 45)
        self.assert_outputs(True, True)
        state = self.tick(100, 49)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assertFalse(state.hold_timer_running)
        state = self.tick(1, 50)
        self.assertEqual(state.auto_phase, AutoPhase.HOLDING)
        self.assertEqual(state.drying_remaining_s, 10)
        self.assertEqual(self.tick(2, 49).drying_elapsed_s, 2)

    def test_time_pauses_below_and_above_band_without_resetting_progress(self):
        self.start_hold()
        self.assertEqual(self.tick(2, 50).drying_elapsed_s, 2)
        state = self.tick(3, 47.9)
        self.assertEqual(state.drying_elapsed_s, 2)
        self.assertFalse(state.hold_timer_running)
        self.assert_outputs(True, True)
        self.assertEqual(self.tick(20, 47).drying_elapsed_s, 2)
        state = self.tick(4, 48)
        self.assertEqual(state.drying_elapsed_s, 2)  # Không cộng khoảng quay lại vùng.
        self.assertTrue(state.hold_timer_running)
        self.assertEqual(self.tick(1, 49).drying_elapsed_s, 3)
        state = self.tick(2, 53)
        self.assertEqual(state.drying_elapsed_s, 3)
        self.assertFalse(state.hold_timer_running)
        self.assert_outputs(False, True)
        self.assertEqual(self.tick(5, 52).drying_elapsed_s, 3)
        state = self.tick(7, 51)
        self.assertEqual(state.auto_phase, AutoPhase.COOLING)
        self.assertEqual(state.drying_elapsed_s, 10)
        self.assertEqual(state.drying_remaining_s, 0)

    def test_band_boundaries_count_and_heater_uses_hysteresis(self):
        self.start_hold()
        self.assertEqual(self.tick(1, 48).drying_elapsed_s, 1)
        self.assert_outputs(True, True)
        self.tick(1, 49)
        self.assert_outputs(True, True)
        self.tick(1, 50)
        self.assert_outputs(False, True)
        state = self.tick(1, 52)
        self.assertEqual(state.drying_elapsed_s, 4)
        self.assertTrue(state.hold_timer_running)

    def test_cooling_never_restarts_heater_and_completes_only_at_threshold(self):
        self.start_hold()
        self.assertEqual(self.tick(10, 50).auto_phase, AutoPhase.COOLING)
        self.assert_outputs(False, True)
        self.assertEqual(self.tick(100, 36).auto_phase, AutoPhase.COOLING)
        self.assert_outputs(False, True)
        state = self.tick(1, 35)
        self.assertEqual(state.auto_phase, AutoPhase.COMPLETE)
        self.assertFalse(state.hold_timer_running)
        self.assert_outputs(False, False)
        self.assertEqual(self.tick(1000, 20).auto_phase, AutoPhase.COMPLETE)
        self.assert_outputs(False, False)

    def test_new_batch_requires_explicit_start_and_resets_timer(self):
        self.start_hold()
        self.tick(10, 50)
        self.tick(1, 35)
        self.assertEqual(self.command("START_DRYING").status_code, 200)
        state = self.tick(0, 28)
        self.assertEqual(state.auto_phase, AutoPhase.HEATING)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assertEqual(state.drying_remaining_s, 10)

    def test_hmi_duration_in_seconds_controls_the_actual_batch(self):
        self.assertEqual(self.command("SET_DRYING_DURATION", 120).status_code, 200)
        self.start_hold()
        self.tick(119, 50)
        payload = self.client.get("/api/state").get_json()
        self.assertEqual(payload["settings"]["drying_duration_s"], 120)
        self.assertEqual(payload["state"]["drying_remaining_s"], 1)
        self.assertTrue(payload["state"]["hold_timer_running"])
        self.assertEqual(self.tick(1, 50).auto_phase, AutoPhase.COOLING)

    def test_hmi_cooling_threshold_controls_completion(self):
        self.assertEqual(self.command("SET_COOLING_TEMPERATURE", 30).status_code, 200)
        self.start_hold()
        self.tick(10, 50)
        self.assertEqual(self.tick(1, 35).auto_phase, AutoPhase.COOLING)
        self.assertEqual(self.tick(1, 30).auto_phase, AutoPhase.COMPLETE)

    def test_active_batch_rejects_recipe_changes_and_repeated_starts(self):
        self.start_hold()
        self.tick(2, 50)
        for kind, value in (
            ("SET_DRYING_DURATION", 20),
            ("SET_TEMPERATURE", 55),
            ("SET_COOLING_TEMPERATURE", 30),
            ("START_DRYING", None),
            ("SET_MODE", "AUTO"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(self.command(kind, value).status_code, 409)
        self.assertEqual(self.tick(1, 50).drying_elapsed_s, 3)
        self.assertEqual(self.controller.get_settings_snapshot(), self.settings)

    def test_cooling_also_locks_recipe_and_start_commands(self):
        self.start_hold()
        self.tick(10, 50)
        self.assertEqual(self.command("SET_DRYING_DURATION", 20).status_code, 409)
        self.assertEqual(self.command("START_DRYING").status_code, 409)

    def test_invalid_duration_and_cooling_values_preserve_previous_settings(self):
        for value in (True, False, None, "10", 0, -1, float("nan"), float("inf")):
            with self.subTest(duration=value):
                self.assertEqual(
                    self.command("SET_DRYING_DURATION", value).status_code, 409
                )
        for value in (True, None, "35", -1, 48, 50, float("nan"), float("inf")):
            with self.subTest(cooling=value):
                self.assertEqual(
                    self.command("SET_COOLING_TEMPERATURE", value).status_code, 409
                )
        self.assertEqual(self.command("SET_TEMPERATURE", 36).status_code, 409)
        self.assertEqual(self.controller.get_settings_snapshot(), self.settings)

    def test_off_aborts_and_clears_progress_without_resuming(self):
        self.start_hold()
        self.tick(2, 50)
        self.assertEqual(self.command("SET_MODE", "OFF").status_code, 200)
        state = self.tick(100, 48)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assertEqual(state.auto_phase, AutoPhase.IDLE)
        self.assert_outputs(False, False)

    def test_emergency_stop_cancels_timer_and_reset_requires_new_start(self):
        self.start_hold()
        self.tick(2, 50)
        self.command("EMERGENCY_STOP")
        state = self.tick(100, 50)
        self.assertEqual(state.mode, ControlMode.FAULT)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assertFalse(state.hold_timer_running)
        self.assert_outputs(False, False)
        self.assertEqual(self.command("START_DRYING").status_code, 409)
        self.assertEqual(self.command("RESET_ALARM").status_code, 200)
        self.assertEqual(self.tick(100, 50).mode, ControlMode.OFF)

    def test_restart_does_not_restore_incomplete_drying_timer(self):
        self.start_hold()
        state = self.tick(2, 50)
        controller = SystemController(
            state, self.settings, self.relay, clock=lambda: self.now
        )
        controller.run_control_cycle()
        state = controller.get_state_snapshot()
        self.assertEqual(state.mode, ControlMode.OFF)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assertFalse(self.relay.process_fan_on)

    def test_batch_can_start_at_setpoint_without_heater_pulse(self):
        self.start_hold()
        self.assertNotIn(("set_heater", True), self.relay.command_history)
        self.assertEqual(self.tick(1, 50).drying_elapsed_s, 1)

    def test_hot_start_waits_for_temperature_to_enter_hold_band(self):
        self.command("START_DRYING")
        state = self.tick(0, 55)
        self.assertEqual(state.auto_phase, AutoPhase.HOLDING)
        self.assertFalse(state.hold_timer_running)
        self.assertEqual(self.tick(100, 54).drying_elapsed_s, 0)
        self.assertEqual(self.tick(10, 52).drying_elapsed_s, 0)
        self.assertEqual(self.tick(2, 51).drying_elapsed_s, 2)

    def test_heater_failure_during_hold_invalidates_batch(self):
        self.start_hold()
        self.relay.fail_operations.add("set_heater")
        state = self.tick(1, 47)
        self.assertEqual(state.mode, ControlMode.FAULT)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assertFalse(state.hold_timer_running)

    def test_bus_loss_cancels_progress_and_does_not_count_offline_time(self):
        self.start_hold()
        self.tick(2, 50)
        self.controller.update_measurement(None, False, False)
        self.controller.run_control_cycle()
        state = self.tick(100, 50)
        self.assertEqual(state.alarm_code, AlarmCode.MODBUS_COMMUNICATION_LOST)
        self.assertEqual(state.mode, ControlMode.FAULT)
        self.assertEqual(state.drying_elapsed_s, 0)
        self.assert_outputs(False, False)

    def test_editing_completed_batch_returns_to_ready_off_state(self):
        self.start_hold()
        self.tick(10, 50)
        self.tick(1, 35)
        self.assertEqual(self.command("SET_DRYING_DURATION", 20).status_code, 200)
        state = self.controller.get_state_snapshot()
        self.assertEqual(state.mode, ControlMode.OFF)
        self.assertEqual(state.drying_remaining_s, 20)
        self.assertEqual(state.drying_elapsed_s, 0)

    def test_start_command_rejects_payload_and_stale_remote_request(self):
        self.assertEqual(self.command("START_DRYING", True).status_code, 409)
        result = self.controller.process_command(
            ControlCommand(
                CommandType.START_DRYING,
                CommandSource.REMOTE_SCADA,
                timestamp_ms=0,
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(self.controller.get_state_snapshot().mode, ControlMode.OFF)

    def test_fractional_duration_completes_without_negative_remaining(self):
        self.command("SET_DRYING_DURATION", 0.3)
        self.start_hold()
        for _ in range(3):
            state = self.tick(0.1, 50)
        self.assertEqual(state.auto_phase, AutoPhase.COOLING)
        self.assertEqual(state.drying_remaining_s, 0)
        self.assertEqual(state.drying_elapsed_s, 0.3)

    def test_zero_delay_starts_fan_with_first_heating_cycle(self):
        controller = SystemController(
            SystemState(),
            make_settings(heater_to_fan_start_delay_s=0),
            self.relay,
            clock=lambda: self.now,
        )
        controller.update_measurement(28, True, True)
        controller.run_control_cycle()
        controller.process_command(
            ControlCommand(CommandType.START_DRYING, CommandSource.SYSTEM)
        )
        controller.run_control_cycle()
        self.assertTrue(self.relay.heater_on)
        self.assertTrue(self.relay.process_fan_on)
