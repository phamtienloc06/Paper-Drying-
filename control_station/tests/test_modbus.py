import unittest
from unittest.mock import patch

from control_station.modbus import ModbusCommunicationError, ModbusDeviceError, ModbusRTU


class FakeResponse:
    def __init__(self, registers=None, bits=None, error=False):
        self.registers = registers or []
        self.bits = bits or []
        self.error = error

    def isError(self):
        return self.error


class FakeModbusClient:
    def __init__(self):
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.read_devices = []
        self.write_calls = []
        self.read_failures = 0

    def connect(self):
        self.connect_calls += 1
        return True

    def close(self):
        self.close_calls += 1

    def read_holding_registers(self, address, count, device_id):
        self.read_calls += 1
        self.read_devices.append(device_id)
        if self.read_failures:
            self.read_failures -= 1
            raise OSError("temporary serial failure")
        return FakeResponse(registers=[253, 421])

    def write_coil(self, address, value, device_id):
        self.write_calls.append((address, value, device_id))
        return FakeResponse()

    def write_register(self, address, value, device_id):
        self.write_calls.append((address, value, device_id))
        return FakeResponse()

    def write_registers(self, address, values, device_id, no_response_expected=False):
        self.write_calls.append((address, list(values), device_id, no_response_expected))
        return FakeResponse()


class TestModbusRTU(unittest.TestCase):
    def make_modbus(self, client, retries=0):
        config = {
            "port": "COM12",
            "baudrate": 9600,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "timeout_ms": 1000,
            "retries": retries,
            "retry_delay_ms": 0,
        }
        return ModbusRTU(config, client=client)

    def test_reads_pt100_registers_from_configured_slave(self):
        client = FakeModbusClient()
        modbus = self.make_modbus(client)

        values = modbus.read_holding_registers(slave_id=2, address=0, count=2)

        self.assertEqual(values, [253, 421])
        self.assertEqual(client.read_calls, 1)

    def test_retries_failed_transaction_five_times_after_first_attempt(self):
        client = FakeModbusClient()
        client.read_failures = 5
        modbus = self.make_modbus(client, retries=5)

        values = modbus.read_holding_registers(slave_id=2, address=0)

        self.assertEqual(values, [253])
        self.assertEqual(client.read_calls, 6)

    def test_writes_relay_coil_to_configured_slave(self):
        client = FakeModbusClient()
        modbus = self.make_modbus(client)

        modbus.write_coil(slave_id=1, address=1, enabled=True)

        self.assertEqual(client.write_calls, [(1, True, 1)])

    def test_writes_pt100_device_id_register_at_current_default_id(self):
        client = FakeModbusClient()
        modbus = self.make_modbus(client)

        modbus.write_register(slave_id=255, address=0x0002, value=2)

        self.assertEqual(client.write_calls, [(0x0002, 2, 255)])

    def test_rejects_broadcast_for_normal_read(self):
        modbus = self.make_modbus(FakeModbusClient())

        with self.assertRaises(ValueError):
            modbus.read_coils(slave_id=0, address=0)
        with self.assertRaises(ValueError):
            modbus.read_holding_registers(slave_id=0, address=0)

    def test_explicit_broadcast_reads_relay_device_id_register(self):
        client = FakeModbusClient()
        modbus = self.make_modbus(client)

        values = modbus.read_holding_registers(
            slave_id=0, address=0, allow_broadcast=True,
        )

        self.assertEqual(values, [253])
        self.assertEqual(client.read_devices, [0])

    def test_writes_multiple_registers_to_configure_relay_id(self):
        client = FakeModbusClient()
        modbus = self.make_modbus(client)

        modbus.write_registers(slave_id=255, address=0x0000, values=[1])

        self.assertEqual(client.write_calls, [(0, [1], 255, False)])

    def test_broadcast_write_requires_explicit_no_response(self):
        client = FakeModbusClient()
        modbus = self.make_modbus(client)

        with self.assertRaises(ValueError):
            modbus.write_registers(slave_id=0, address=0, values=[1])

        modbus.write_registers(
            slave_id=0, address=0, values=[1], no_response_expected=True,
        )
        self.assertEqual(client.write_calls, [(0, [1], 0, True)])

    def test_timeout_logs_each_attempt_and_preserves_context_in_final_error(self):
        client = FakeModbusClient()
        client.read_failures = 4
        modbus = self.make_modbus(client, retries=3)

        with self.assertLogs("control_station.modbus", level="WARNING") as logs:
            with self.assertRaises(ModbusCommunicationError) as caught:
                modbus.read_holding_registers(2, 0, count=1)

        context = "port=COM12 ID=2 FC=0x03 address=0x0000 count=1"
        self.assertEqual(len(logs.output), 4)
        for attempt, message in enumerate(logs.output, start=1):
            self.assertIn(context, message)
            self.assertIn(f"attempt={attempt}/4", message)
            self.assertIn("elapsed_ms=", message)
        self.assertIn(context, str(caught.exception))
        self.assertIn("failed after 4 attempts", str(caught.exception))
        self.assertEqual(client.read_calls, 4)

    def test_failed_requests_log_correct_function_address_and_quantity(self):
        cases = [
            ("read_holding_registers", (2, 3, 2), 0x03, 2, 3, 2),
            ("read_input_registers", (2, 3, 2), 0x04, 2, 3, 2),
            ("read_coils", (1, 0, 8), 0x01, 1, 0, 8),
            ("write_register", (2, 6, 0), 0x06, 2, 6, 1),
            ("write_registers", (1, 0, [1, 2]), 0x10, 1, 0, 2),
            ("write_coil", (1, 1, False), 0x05, 1, 1, 1),
            ("write_coils", (1, 0, [False, False]), 0x0F, 1, 0, 2),
        ]
        for method, args, fc, device_id, address, count in cases:
            with self.subTest(method=method):
                client = FakeModbusClient()
                modbus = self.make_modbus(client)
                with (
                    patch.object(client, method, create=True, side_effect=OSError("timeout")),
                    self.assertLogs("control_station.modbus", level="WARNING") as logs,
                    self.assertRaises(ModbusCommunicationError),
                ):
                    getattr(modbus, method)(*args)
                self.assertIn(
                    f"ID={device_id} FC=0x{fc:02X} address=0x{address:04X} count={count}",
                    logs.output[0],
                )
                self.assertIn("attempt=1/1", logs.output[0])

    def test_device_rejection_is_logged_without_retrying(self):
        client = FakeModbusClient()
        response = FakeResponse(error=True)
        response.exception_code = 2
        modbus = self.make_modbus(client, retries=3)
        with (
            patch.object(client, "read_holding_registers", return_value=response) as read,
            self.assertLogs("control_station.modbus", level="WARNING") as logs,
            self.assertRaises(ModbusDeviceError),
        ):
            modbus.read_holding_registers(2, 6)
        read.assert_called_once()
        self.assertEqual(client.close_calls, 0)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("no retry", logs.output[0])
        self.assertIn("ID=2 FC=0x03 address=0x0006 count=1", logs.output[0])
        self.assertIn("exception code 2", logs.output[0])

    def test_success_after_retry_reports_recovery(self):
        client = FakeModbusClient()
        client.read_failures = 1
        modbus = self.make_modbus(client, retries=3)
        with self.assertLogs("control_station.modbus", level="INFO") as logs:
            self.assertEqual(modbus.read_holding_registers(2, 0), [253])
        self.assertEqual(len(logs.output), 2)
        self.assertIn("Modbus attempt failed", logs.output[0])
        self.assertIn("Modbus recovered", logs.output[1])
        self.assertIn("ID=2 FC=0x03 address=0x0000 count=1", logs.output[1])
        self.assertIn("attempt=2/4", logs.output[1])

    def test_probe_retry_override_is_local_and_does_not_reduce_write_retries(self):
        client = FakeModbusClient()
        bus = self.make_modbus(client, retries=3)
        with (
            patch.object(client, "read_coils", create=True, side_effect=OSError("timeout")) as read,
            self.assertLogs("control_station.modbus", level="WARNING") as logs,
            self.assertRaises(ModbusCommunicationError),
        ):
            bus.read_coils(1, 0, 8, retries=0)
        read.assert_called_once()
        self.assertIn("attempt=1/1", logs.output[0])
        self.assertEqual(bus.retries, 3)
        with patch.object(
            client, "write_coil",
            side_effect=[OSError("timeout"), OSError("timeout"), FakeResponse()],
        ) as write:
            with self.assertLogs("control_station.modbus", level="WARNING"):
                bus.write_coil(1, 0, False)
        self.assertEqual(write.call_count, 3)

    def test_invalid_probe_retry_budget_is_rejected_before_io(self):
        client = FakeModbusClient()
        bus = self.make_modbus(client, retries=3)
        for retries in (-1, True, 1.5):
            with self.subTest(retries=retries), self.assertRaises(ValueError):
                bus.read_coils(1, 0, 8, retries=retries)
        self.assertEqual(client.connect_calls, 0)


if __name__ == "__main__":
    unittest.main()
