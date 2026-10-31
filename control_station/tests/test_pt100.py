"""Kiểm tra driver PT100 bằng bus giả, không mở COM hay ghi thiết bị."""

import unittest
from unittest.mock import Mock

from control_station.modbus import ModbusCommunicationError
from control_station.pt100 import PT100Sensor, PT100DataError


class TestPT100Sensor(unittest.TestCase):
    def test_change_id_is_explicit_and_uses_configured_register(self):
        bus = Mock()
        config = dict(
            slave_id=2,
            temperature_register=0,
            data_type="int16",
            scale=0.1,
            minimum_temperature_c=-200,
            maximum_temperature_c=850,
            device_id_register=2,
        )
        sensor = PT100Sensor(bus, config)
        bus.write_register.assert_not_called()
        self.assertTrue(sensor.change_slave_id(1))
        bus.write_register.assert_called_once_with(slave_id=1, address=2, value=2)

    def test_matching_id_does_not_write_and_invalid_id_is_rejected(self):
        self.assertFalse(self.sensor.change_slave_id(2))
        self.bus.write_register.assert_not_called()
        for value in (0, 256, True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.sensor.change_slave_id(value)

    def setUp(self):
        self.bus = Mock()
        self.config = dict(
            slave_id=2,
            temperature_register=0,
            data_type="int16",
            scale=0.1,
            minimum_temperature_c=-200,
            maximum_temperature_c=850,
            device_id_register=2,
        )
        self.sensor = PT100Sensor(self.bus, self.config)

    def test_decode_and_address(self):
        for raw, expected in (
            (255, 25.5),
            (600, 60),
            (0xFF9C, -10),
            (0, 0),
            (63536, -200),
            (8500, 850),
        ):
            with self.subTest(raw=raw):
                self.bus.read_holding_registers.return_value = [raw]
                self.assertEqual(self.sensor.read_sample(), (raw, expected))
        self.bus.read_holding_registers.assert_called_with(
            slave_id=2, address=0, count=1
        )
        self.bus.connect.assert_not_called()
        self.bus.write_coil.assert_not_called()

    def test_unsigned_decoding(self):
        sensor = PT100Sensor(
            self.bus, {**self.config, "data_type": "uint16", "scale": 0.01}
        )
        self.bus.read_holding_registers.return_value = [0xFFFF]
        self.assertAlmostEqual(sensor.read_temperature(), 655.35)

    def test_invalid_response_and_range(self):
        for values in ([], [1, 2], [-1], [65536], [True], ["600"], [8501], [63535]):
            with self.subTest(values=values):
                self.bus.read_holding_registers.return_value = values
                with self.assertRaises(PT100DataError):
                    self.sensor.read_temperature()

    def test_read_failure_does_not_return_previous_sample(self):
        self.bus.read_holding_registers.return_value = [600]
        self.assertEqual(self.sensor.read_temperature(), 60)
        self.bus.read_holding_registers.side_effect = ModbusCommunicationError(
            "timeout"
        )
        with self.assertRaises(ModbusCommunicationError):
            self.sensor.read_temperature()

    def test_custom_limits(self):
        sensor = PT100Sensor(
            self.bus,
            {**self.config, "minimum_temperature_c": 0, "maximum_temperature_c": 100},
        )
        self.bus.read_holding_registers.return_value = [1001]
        with self.assertRaises(PT100DataError):
            sensor.read_temperature()

    def test_invalid_configuration(self):
        for changes in (
            {"slave_id": 0},
            {"slave_id": 248},
            {"temperature_register": -1},
            {"temperature_register_count": 2},
            {"temperature_function_code": 4},
            {"data_type": "float32"},
            {"scale": 0},
            {"scale": float("nan")},
            {"minimum_temperature_c": 1000},
            {"maximum_temperature_c": float("inf")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PT100Sensor(self.bus, {**self.config, **changes})


if __name__ == "__main__":
    unittest.main()
