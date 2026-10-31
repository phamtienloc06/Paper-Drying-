"""Kiểm thử các lỗi phát hiện khi rà soát, không giao tiếp phần cứng."""

import unittest
from unittest.mock import patch
from control_station.modbus import ModbusRTU, ModbusDeviceError
from test_modbus import FakeModbusClient, FakeResponse


class ReviewRegressions(unittest.TestCase):
    def bus(self, client, **extra):
        return ModbusRTU(
            dict(port="FAKE", baudrate=9600, retries=1, retry_delay_ms=0, **extra),
            client=client,
        )

    def test_preserves_fractional_stopbits(self):
        self.assertEqual(self.bus(FakeModbusClient(), stopbits=1.5).stopbits, 1.5)

    def test_register_rejects_non_integers_without_io(self):
        client = FakeModbusClient()
        for value in (True, 1.5, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bus(client).write_register(1, 0, value)
        self.assertEqual(client.write_calls, [])

    def test_incomplete_response_retries(self):
        client = FakeModbusClient()
        with patch.object(
            client,
            "read_holding_registers",
            side_effect=[FakeResponse(registers=[]), FakeResponse(registers=[123])],
        ) as read:
            self.assertEqual(self.bus(client).read_holding_registers(1, 0), [123])
            self.assertEqual(read.call_count, 2)

    def test_close_error_does_not_interrupt_retry(self):
        client = FakeModbusClient()
        client.read_failures = 1
        with patch.object(client, "close", side_effect=OSError("close failed")):
            self.assertEqual(self.bus(client).read_holding_registers(1, 0), [253])
        self.assertEqual(client.read_calls, 2)

    def test_invalid_address_exception_is_not_retried(self):
        client = FakeModbusClient()
        response = FakeResponse(error=True)
        response.exception_code = 2
        with patch.object(
            client, "read_holding_registers", return_value=response
        ) as read:
            with self.assertRaises(ModbusDeviceError):
                self.bus(client).read_holding_registers(1, 0)
            self.assertEqual(read.call_count, 1)

    def test_busy_exception_can_retry(self):
        client = FakeModbusClient()
        response = FakeResponse(error=True)
        response.exception_code = 6
        with patch.object(
            client,
            "read_holding_registers",
            side_effect=[response, FakeResponse(registers=[123])],
        ) as read:
            self.assertEqual(self.bus(client).read_holding_registers(1, 0), [123])
            self.assertEqual(read.call_count, 2)

    def test_relay_diagnostic_normalizes_inverted_coil_bits(self):
        from control_station.tests.relay_sequence import read_relay_states

        client = unittest.mock.Mock()
        client.read_coils.return_value = [False, True] + [False] * 6
        config = dict(
            slave_id=2,
            channels=dict(relay_1_coil=0, relay_2_coil=1),
            status_coil_count=8,
            status_readback={"on": True, "off": False},
        )
        self.assertEqual(read_relay_states(client, config), [False, True])
        client.read_coils.assert_called_once_with(slave_id=2, address=0, count=8)

    def test_relay_channel_diagnostic_reads_eight_coils_and_selects_channel_bit(self):
        from control_station.tests.relay_sequence import read_relay_channel_state

        client = unittest.mock.Mock()
        client.read_coils.return_value = [False, True] + [False] * 6
        config = dict(
            slave_id=1,
            channels=dict(relay_1_coil=0, relay_2_coil=1),
            status_coil_count=8,
            status_readback={"on": True, "off": False},
        )

        self.assertTrue(read_relay_channel_state(client, config, 2))
        client.read_coils.assert_called_once_with(slave_id=1, address=0, count=8)

    def test_relay_id_scan_uses_read_only_broadcast_at_configured_baud(self):
        from control_station.tests.relay_sequence import scan_relay_id

        client = unittest.mock.Mock()
        client.read_holding_registers.return_value = [255]
        modbus_config = dict(
            port="FAKE",
            baudrate=9600,
            timeout_ms=1000,
            retries=2,
            retry_delay_ms=100,
        )
        relay_config = dict(
            device_id_register=0,
            device_id_read_broadcast_slave_id=0,
            supported_baudrates=[9600],
        )

        with patch(
            "control_station.tests.relay_sequence.ModbusRTU", return_value=client
        ) as modbus_factory:
            self.assertEqual(scan_relay_id(modbus_config, relay_config), (255, 9600))

        modbus_factory.assert_called_once()
        client.connect.assert_called_once()
        client.read_holding_registers.assert_called_once_with(
            slave_id=0, address=0, count=1, allow_broadcast=True
        )
        client.close.assert_called_once()

    def test_setting_relay_id_broadcasts_then_verifies_new_id(self):
        from control_station.tests.relay_sequence import set_relay_id

        writer = unittest.mock.Mock()
        writer.read_holding_registers.return_value = [255]
        verifier = unittest.mock.Mock()
        verifier.read_holding_registers.return_value = [1]
        config = dict(port="FAKE", baudrate=9600)
        relay = dict(
            device_id_register=0,
            device_id_broadcast_slave_id=0,
            device_id_read_broadcast_slave_id=0,
            supported_baudrates=[4800, 9600],
        )

        with patch(
            "control_station.tests.relay_sequence.ModbusRTU",
            side_effect=[writer, verifier],
        ) as modbus_factory, patch("control_station.tests.relay_sequence.time.sleep"):
            set_relay_id(config, relay, 1)

        self.assertEqual(modbus_factory.call_count, 2)
        writer.write_registers.assert_called_once_with(
            slave_id=0, address=0, values=[1], no_response_expected=True
        )
        verifier.read_holding_registers.assert_called_once_with(
            slave_id=0, address=0, count=1, allow_broadcast=True
        )
        writer.close.assert_called_once()
        verifier.close.assert_called_once()

    def test_interactive_relay_commands_dispatch_to_read_and_test_modes(self):
        from control_station.tests.relay_sequence import interactive_mode

        with (
            patch(
                "control_station.tests.relay_sequence.load_configuration",
                return_value=({"port": "FAKE"}, {"slave_id": 1}),
            ),
            patch(
                "builtins.input",
                side_effect=["doc", "docrelay1-2", "battat", "thoat"],
            ),
            patch("control_station.tests.relay_sequence.scan_relay_id") as scan_id,
            patch(
                "control_station.tests.relay_sequence.run_hardware_test"
            ) as run_test,
        ):
            self.assertEqual(interactive_mode(), 0)

        scan_id.assert_called_once_with({"port": "FAKE"}, {"slave_id": 1})
        self.assertEqual(
            run_test.call_args_list,
            [
                unittest.mock.call(
                    {"port": "FAKE"}, {"slave_id": 1}, check_only=True
                ),
                unittest.mock.call({"port": "FAKE"}, {"slave_id": 1}),
            ],
        )
