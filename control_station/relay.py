"""Adapter relay: API dùng trạng thái tải; FC05/FC01 dùng trạng thái cuộn relay."""

from collections.abc import Mapping
from typing import Any
from .modbus import ModbusRTU


class RelayModule:
    def __init__(self, modbus: ModbusRTU, config: Mapping[str, Any]) -> None:
        self._modbus = modbus
        self._slave_id = self._integer(config.get("slave_id"), "slave_id", 1, 247)
        channels, outputs, status = (
            config.get("channels"),
            config.get("outputs"),
            config.get("status_readback"),
        )
        if not all(isinstance(value, Mapping) for value in (channels, outputs, status)):
            raise ValueError("Cấu hình relay cần channels, outputs và status_readback.")
        self._on_raw, self._off_raw = status.get("on"), status.get("off")
        if (
            type(self._on_raw) is not bool
            or type(self._off_raw) is not bool
            or self._on_raw == self._off_raw
        ):
            raise ValueError("status_readback.on/off phải là hai bool khác nhau.")
        self._outputs = {}
        for name in ("heater", "process_fan"):
            output = outputs.get(name)
            if not isinstance(output, Mapping) or output.get("contact") not in (
                "NO",
                "NC",
            ):
                raise ValueError(f"relay.outputs.{name} cần channel và contact NO/NC.")
            channel = output.get("channel")
            if channel not in ("relay_1", "relay_2"):
                raise ValueError(f"Kênh relay {name} không hợp lệ.")
            coil = self._integer(channels.get(channel + "_coil"), channel, 0, 65535)
            self._outputs[name] = (coil, output["contact"])
        if self._outputs["heater"][0] == self._outputs["process_fan"][0]:
            raise ValueError("Gia nhiệt và quạt phải dùng hai coil khác nhau.")
        self._status_coil_count = self._integer(
            config.get("status_coil_count", 1), "status_coil_count", 1, 2000
        )
        first_coil = min(coil for coil, _ in self._outputs.values())
        if any(
            coil - first_coil >= self._status_coil_count
            for coil, _ in self._outputs.values()
        ):
            raise ValueError("status_coil_count không bao phủ các coil relay.")
        self._last_known_state = {"heater": None, "process_fan": None}
        self._communication_status: bool | None = None

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(
                f"relay {name} phải là số nguyên trong {minimum}..{maximum}."
            )
        return value

    def _read_output(self, name: str) -> bool:
        coil, contact = self._outputs[name]
        first_coil = min(value for value, _ in self._outputs.values())
        try:
            values = self._modbus.read_coils(
                slave_id=self._slave_id,
                address=first_coil,
                count=self._status_coil_count,
            )
            offset = coil - first_coil
            if len(values) <= offset or any(type(value) is not bool for value in values):
                raise RuntimeError(f"Phản hồi trạng thái relay {name} không hợp lệ.")
        except Exception:
            self._communication_status = False
            raise
        self._communication_status = True
        energized = values[offset] == self._on_raw
        enabled = energized if contact == "NO" else not energized
        self._last_known_state[name] = enabled
        return enabled

    def read_states(self, *, retries: int | None = None) -> tuple[bool, bool]:
        """Trạng thái tải suy ra từ coil và NO/NC, chưa phải đo điện áp tải."""
        coils = [self._outputs[name][0] for name in ("heater", "process_fan")]
        first = min(coils)
        try:
            values = self._modbus.read_coils(
                slave_id=self._slave_id,
                address=first,
                count=self._status_coil_count,
                **({"retries": retries} if retries is not None else {}),
            )
            if len(values) < self._status_coil_count or any(
                type(value) is not bool for value in values
            ):
                raise RuntimeError("Phản hồi trạng thái các coil relay không hợp lệ.")
        except Exception:
            self._communication_status = False
            raise
        states = {}
        for name, (coil, contact) in self._outputs.items():
            energized = values[coil - first] == self._on_raw
            states[name] = energized if contact == "NO" else not energized
        self._last_known_state = states
        self._communication_status = True
        return states["heater"], states["process_fan"]

    def set_output(self, name: str, enabled: bool) -> None:
        if name not in self._outputs:
            raise ValueError(f"Đầu ra không hỗ trợ: {name!r}.")
        if type(enabled) is not bool:
            raise TypeError("Trạng thái tải phải là bool.")
        coil, contact = self._outputs[name]
        energized = enabled if contact == "NO" else not enabled
        try:
            self._modbus.write_coil(
                slave_id=self._slave_id, address=coil, enabled=energized
            )
        except Exception:
            self._communication_status = False
            raise
        if self._read_output(name) != enabled:
            raise RuntimeError(f"Relay {name} state confirmation failed.")

    def set_heater(self, enabled: bool) -> None:
        self.set_output("heater", enabled)

    def set_process_fan(self, enabled: bool) -> None:
        self.set_output("process_fan", enabled)

    def all_outputs_off(self) -> None:
        errors = []
        for name in self._outputs:
            try:
                self.set_output(name, False)
            except Exception as error:
                errors.append(f"{name}: {error}")
        if errors:
            raise RuntimeError(
                "Không xác nhận được tất cả tải OFF: " + "; ".join(errors)
            )

    @property
    def last_known_state(self) -> dict:
        return dict(self._last_known_state)

    @property
    def slave_id(self) -> int:
        return self._slave_id

    @property
    def communication_status(self) -> bool | None:
        """Phản hồi module cuối: None chưa kiểm tra, True hợp lệ, False lỗi."""
        return self._communication_status

    def mark_disconnected(self) -> None:
        self._communication_status = False
