import unittest

from app.protocols.tdr import get_frame_basic_info, parse_tdr_payload_hex


def build_frame() -> bytes:
    raw = bytearray(4096)
    raw[0] = 0xAA
    raw[1] = 6  # 最新协议：0x06 表示 B-E 相间
    raw[2] = 99  # Byte2 不参与电缆长度解析。

    # Byte3~22 依次保存 10 组相间故障距离，这里给 B-E 写入 450 m。
    raw[15:17] = (450).to_bytes(2, "big")

    # Byte23~32 依次保存 10 组故障类型，0=正常、1=开路、2=短路。
    raw[29] = 1  # B-E 开路

    # Byte33~4032 是 4000 个原始 uint8 波形点。
    raw[33:4033] = bytes([128]) * 4000
    # 预留字段前两个字节按 uint16 大端序保存设备标注长度，单位为 m。
    raw[4033:4035] = (2000).to_bytes(2, "big")
    raw[-1] = 0x55
    return bytes(raw)


class TdrProtocolTests(unittest.TestCase):
    def test_parse_4096_byte_frame(self):
        payload_hex = build_frame().hex()
        info = get_frame_basic_info(payload_hex)
        parsed = parse_tdr_payload_hex(payload_hex, phase_pair=info["phase_pair"])

        self.assertTrue(info["valid_start"])
        self.assertTrue(info["valid_end"])
        self.assertTrue(info["valid_length"])
        self.assertEqual(info["phase_pair"], "B-E")
        self.assertEqual(info["device_reported_cable_length_m"], 2000.0)
        self.assertEqual(len(parsed["waveform_4000"]), 4000)
        self.assertEqual(parsed["waveform_4000"][0], 128)
        self.assertEqual(parsed["device_result_all"]["B-E"]["type"], "OPEN")
        self.assertEqual(parsed["device_result_all"]["B-E"]["distance_m"], 450.0)
        self.assertEqual(parsed["device_fault_type"], "OPEN")

    def test_invalid_tail_is_rejected(self):
        raw = bytearray(build_frame())
        raw[-1] = 0x00

        with self.assertRaisesRegex(ValueError, "invalid end byte"):
            parse_tdr_payload_hex(raw.hex())


if __name__ == "__main__":
    unittest.main()
