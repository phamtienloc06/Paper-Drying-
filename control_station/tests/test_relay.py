"""Kiểm thử adapter relay bằng bus Modbus giả, không dùng phần cứng."""

from __future__ import annotations

import unittest

from control_station.relay import RelayModule


class FakeModbus:
    def __init__(
        self,
        bits: list[bool] | None = None,
        *,
        on_raw: bool = False,
        off_raw: bool = True,
    ) -> None:
        self.on_raw = on_raw
        self.off_raw = off_raw
        self.bits = list(bits) if bits is not None else []
        self.bits.extend([off_raw] * max(0, 8 - len(self.bits)))
        self.read_calls: list[tuple[int, int, int]] = []
        self.write_calls: list[tuple[int, int, bool]] = []
        self.fail_reads_at: set[int] = set()
        self.fail_writes_at: set[int] = set()
        self.ignore_writes_at: set[int] = set()

    def read_coils(self, slave_id: int, address: int, count: int = 1) -> list[bool]:
        self.read_calls.append((slave_id, address, count))
        if address in self.fail_reads_at:
            raise OSError(f"read failed at coil {address}")
        return self.bits[address : address + count]

    def write_coil(self, slave_id: int, address: int, enabled: bool) -> None:
        self.write_calls.append((slave_id, address, enabled))
        if address in self.fail_writes_at:
            raise OSError(f"write failed at coil {address}")
        if address not in self.ignore_writes_at:
            self.bits[address] = self.on_raw if enabled else self.off_raw


def make_config(
    *, on_raw: bool = False, off_raw: bool = True, contact="NO"
) -> dict[str, object]:
    return {
        "slave_id": 1,
        "channels": {"relay_1_coil": 0, "relay_2_coil": 1},
        "status_coil_count": 8,
        "status_readback": {"on": on_raw, "off": off_raw},
        "outputs": {
            "heater": {"channel": "relay_1", "contact": contact},
            "process_fan": {"channel": "relay_2", "contact": contact},
        },
    }


class TestRelayModule(unittest.TestCase):
    def test_connection_status_tracks_response_failure_and_recovery(self):
        modbus = FakeModbus()
        relay = RelayModule(modbus, make_config())
        self.assertIsNone(relay.communication_status)
        relay.read_states()
        self.assertTrue(relay.communication_status)
        modbus.fail_reads_at.add(0)
        with self.assertRaises(OSError):
            relay.read_states()
        self.assertFalse(relay.communication_status)
        modbus.fail_reads_at.clear()
        relay.read_states()
        self.assertTrue(relay.communication_status)
        relay.mark_disconnected()
        self.assertFalse(relay.communication_status)

    def test_write_transport_failure_marks_relay_disconnected(self):
        modbus = FakeModbus()
        relay = RelayModule(modbus, make_config())
        relay.read_states()
        modbus.fail_writes_at.add(0)
        with self.assertRaises(OSError):
            relay.set_heater(True)
        self.assertFalse(relay.communication_status)

    def test_valid_response_with_command_mismatch_is_not_transport_loss(self):
        modbus = FakeModbus()
        modbus.ignore_writes_at.add(0)
        relay = RelayModule(modbus, make_config())
        with self.assertRaises(RuntimeError):
            relay.set_heater(True)
        self.assertTrue(relay.communication_status)
        self.assertFalse(relay.last_known_state["heater"])

    def test_process_fan_write_is_read_back_and_cached(self) -> None:
        modbus = FakeModbus()
        relay = RelayModule(modbus, make_config())

        relay.set_process_fan(True)

        self.assertEqual(modbus.write_calls, [(1, 1, True)])
        self.assertEqual(modbus.read_calls, [(1, 0, 8)])
        self.assertTrue(relay.last_known_state["process_fan"])

    def test_nc_load_off_energizes_coil_and_readback_confirms_off(self) -> None:
        modbus = FakeModbus()
        relay = RelayModule(modbus, make_config(contact="NC"))

        relay.set_process_fan(False)

        self.assertEqual(modbus.write_calls, [(1, 1, True)])
        self.assertFalse(relay.last_known_state["process_fan"])

    def test_read_states_maps_raw_bits_and_channel_order(self) -> None:
        modbus = FakeModbus(bits=[False, True])
        config = make_config()
        config["outputs"]["heater"]["channel"] = "relay_2"
        config["outputs"]["process_fan"]["channel"] = "relay_1"
        relay = RelayModule(modbus, config)

        self.assertEqual(relay.read_states(), (False, True))
        self.assertEqual(relay.last_known_state["heater"], False)
        self.assertTrue(relay.last_known_state["process_fan"])

    def test_mismatched_readback_raises_instead_of_reporting_success(self) -> None:
        modbus = FakeModbus()
        modbus.ignore_writes_at.add(1)
        relay = RelayModule(modbus, make_config())

        with self.assertRaisesRegex(RuntimeError, "confirmation failed"):
            relay.set_process_fan(True)

    def test_all_outputs_off_attempts_both_relays_if_one_write_fails(self) -> None:
        modbus = FakeModbus(bits=[False, False])
        modbus.fail_writes_at.add(0)
        relay = RelayModule(modbus, make_config())

        with self.assertRaisesRegex(RuntimeError, "heater"):
            relay.all_outputs_off()

        self.assertEqual(modbus.write_calls, [(1, 0, False), (1, 1, False)])
        self.assertEqual(modbus.read_calls, [(1, 0, 8)])
        self.assertFalse(relay.last_known_state["process_fan"])

    def test_unsupported_output_name_does_not_select_a_relay(self) -> None:
        modbus = FakeModbus()
        relay = RelayModule(modbus, make_config())

        with self.assertRaises(ValueError):
            relay.set_output("unknown", True)

        self.assertEqual(modbus.write_calls, [])


if __name__ == "__main__":
    unittest.main()
