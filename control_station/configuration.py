"""Nạp và kiểm tra cấu hình dùng chung cho runtime, HMI và script thiết bị."""

from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any
import yaml
from .models import ControlSettings

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class StationConfiguration:
    modbus: dict[str, Any]
    pt100: dict[str, Any]
    relay: dict[str, Any]
    control: ControlSettings
    poll_interval_s: float
    hmi_host: str
    hmi_port: int
    simulation: dict[str, float]


def load_configuration(path: str | Path = DEFAULT_CONFIG_PATH) -> StationConfiguration:
    """Thông số vận hành bắt buộc có trong YAML; kiểm tra trước khi mở COM."""
    with Path(path).open(encoding="utf-8") as source:
        data = yaml.safe_load(source)
    if not isinstance(data, Mapping):
        raise ValueError("Cấu hình phải là YAML mapping.")
    sections = {}
    for name in ("modbus", "pt100", "relay", "control", "runtime", "hmi", "simulation"):
        if not isinstance(data.get(name), Mapping):
            raise ValueError(f"Thiếu mục cấu hình {name}.")
        sections[name] = dict(data[name])
    try:
        settings = ControlSettings(**sections["control"])
    except TypeError as error:
        raise ValueError(f"Sai trường cấu hình control: {error}") from error
    if sections["pt100"].get("slave_id") == sections["relay"].get("slave_id"):
        raise ValueError("PT100 và relay phải có slave ID khác nhau.")
    interval = sections["runtime"].get("poll_interval_ms")
    if type(interval) is not int or interval <= 0:
        raise ValueError("runtime.poll_interval_ms phải là số nguyên dương.")
    host, port = sections["hmi"].get("host"), sections["hmi"].get("port")
    if (
        not isinstance(host, str)
        or not host.strip()
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise ValueError("Cấu hình HMI host/port không hợp lệ.")
    simulation = sections["simulation"]
    for name in (
        "ambient_c",
        "heater_rate_c_per_s",
        "fan_cooling_rate_c_per_s",
        "ambient_loss_per_s",
    ):
        value = simulation.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"simulation.{name} phải là số hữu hạn.")
        if name != "ambient_c" and value < 0:
            raise ValueError(f"simulation.{name} không được âm.")
    from .modbus import ModbusRTU
    from .pt100 import PT100Sensor
    from .relay import RelayModule

    bus = ModbusRTU(sections["modbus"])
    PT100Sensor(bus, sections["pt100"])
    RelayModule(bus, sections["relay"])
    return StationConfiguration(
        sections["modbus"],
        sections["pt100"],
        sections["relay"],
        settings,
        interval / 1000,
        host,
        port,
        simulation,
    )
