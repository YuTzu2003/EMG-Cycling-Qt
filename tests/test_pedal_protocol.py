import unittest

from delsys.pedal.protocol import decodeCyclingPower


class PedalProtocolTests(unittest.TestCase):
    def test_balance_without_left_reference_is_not_labeled_left(self):
        packet = bytearray((0x01, 0x00, 0xC8, 0x00, 100))
        record, _ = decodeCyclingPower(packet, None)
        self.assertEqual(record["power_w"], 200)
        self.assertIsNone(record["left_balance_percent"])
        self.assertIsNone(record["right_balance_percent"])

    def test_left_reference_allows_estimated_left_right_power(self):
        packet = bytearray((0x03, 0x00, 0xC8, 0x00, 90))
        record, _ = decodeCyclingPower(packet, None)
        self.assertEqual(record["left_balance_percent"], 45)
        self.assertEqual(record["left_power_w"], 90)
        self.assertEqual(record["right_power_w"], 110)
