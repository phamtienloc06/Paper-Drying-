"""Thông số cố định cho unit test; độc lập cấu hình vận hành của người dùng."""

from control_station.models import ControlSettings


def make_settings(**overrides):
    values = dict(
        temperature_setpoint_c=60,
        control_deadband_c=2,
        high_temperature_limit_c=90,
        minimum_setpoint_c=20,
        maximum_setpoint_c=80,
        heater_to_fan_start_delay_s=5,
        drying_duration_s=600,
        cooling_stop_temperature_c=10,
        fan_start_confirmation_timeout_s=10,
    )
    values.update(overrides)
    return ControlSettings(**values)
