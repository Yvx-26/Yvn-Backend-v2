import base64
import unittest

from app.services.payload import normalize_payload_to_hex


class PayloadNormalizationTests(unittest.TestCase):
    def test_explicit_base64_payload(self):
        raw = bytes([0xAA, 0x01, 0xFE, 0x00])
        encoded = base64.b64encode(raw).decode("ascii")

        result = normalize_payload_to_hex(encoded, encoding="base64")

        self.assertEqual(result["hex"], "aa01fe00")
        self.assertEqual(result["length"], 8)
        self.assertEqual(result["byte_length"], 4)

    def test_explicit_hex_payload_ignores_whitespace(self):
        result = normalize_payload_to_hex("AA 01\nFE 00", encoding="hex")

        self.assertEqual(result["hex"], "aa01fe00")
        self.assertEqual(result["encoding"], "hex")


if __name__ == "__main__":
    unittest.main()
