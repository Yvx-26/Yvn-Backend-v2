import unittest

from app.services.waveform import (
    extract_waveform_from_samples,
    payload_to_samples,
)


class WaveformConversionTests(unittest.TestCase):
    def test_extract_new_protocol_waveform(self):
        samples = [0] * 4096
        samples[0] = 0xAA
        samples[1] = 0x00  # A-B 相间
        samples[33:4033] = (list(range(256)) * 16)[:4000]
        samples[-1] = 0x55

        waveform = extract_waveform_from_samples(samples)

        self.assertEqual(len(waveform), 4000)
        self.assertEqual(waveform, samples[33:4033])

    def test_invalid_full_frame_is_not_treated_as_waveform(self):
        samples = [0] * 4096
        samples[0] = 0xAA
        samples[1] = 0x00

        self.assertEqual(extract_waveform_from_samples(samples), [])

    def test_hex_to_full_frame_samples(self):
        self.assertEqual(payload_to_samples("AA00ff"), [0xAA, 0x00, 0xFF])


if __name__ == "__main__":
    unittest.main()
