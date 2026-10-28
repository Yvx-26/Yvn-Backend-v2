"""波形 payload 转采样点的通用工具。

最新设备协议的完整帧固定为 4096 字节：Byte33~4032 是 4000 个
uint8 波形点，Byte4095 是 0x55 帧尾。本模块保留完整帧转换函数，同时
对完整帧执行协议校验，避免把帧头、故障字段或预留区误当成波形。
"""

from typing import Any, List

from app.utils.hex_codec import compact_hex_text

from app.protocols.tdr import (
    TDR_END_BYTE,
    TDR_START_BYTE,
    TDR_TOTAL_BYTES,
    TDR_WAVE_END,
    TDR_WAVE_START,
    phase_pair_from_code,
)


# 十六进制字符白名单。
_HEX_CHARS = set("0123456789abcdefABCDEF")


def _to_text(payload: Any) -> str:
    """把数据库中的 PAYLOAD 统一转为字符串。"""
    if payload is None:
        return ""

    if isinstance(payload, (bytes, bytearray)):
        try:
            return bytes(payload).decode("utf-8")
        except Exception:
            return bytes(payload).hex()

    return str(payload)


def payload_to_samples(payload: Any) -> List[int]:
    """兼容十六进制字符串、bytes 和逗号分隔数字字符串。

    返回完整 payload 字节数组，不在这里截取波形。这样 API 可以同时返回
    原始帧和解析后的 4000 个有效波形点，便于排查设备协议问题。
    """
    text = _to_text(payload).strip()
    if not text:
        return []

    try:
        # 兼容历史数据：数据库可能直接保存 "128,129,130" 一类采样值。
        if "," in text:
            return [int(float(x.strip())) for x in text.split(",") if x.strip() != ""]

        hx = compact_hex_text(text)
        if len(hx) % 2 == 1:
            hx = hx[:-1]

        if hx and len(hx) % 2 == 0 and all(c in _HEX_CHARS for c in hx):
            return [int(hx[i:i + 2], 16) for i in range(0, len(hx), 2)]
    except Exception:
        return []

    return []


def extract_waveform_from_samples(samples: List[int]) -> List[int]:
    """从完整帧提取 Byte33~4032 的 4000 个原始 uint8 波形点。

    长度恰好为 4096 时，必须同时满足帧头、帧尾和相间编码要求，否则返回
    空数组，防止无效帧被前端误画。非 4096 字节数据仍按历史“纯波形数组”
    兼容返回。
    """
    arr = samples if isinstance(samples, list) else []
    if len(arr) == TDR_TOTAL_BYTES:
        valid_frame = (
            arr[0] == TDR_START_BYTE
            and arr[-1] == TDR_END_BYTE
            and phase_pair_from_code(arr[1]) is not None
        )
        return arr[TDR_WAVE_START:TDR_WAVE_END] if valid_frame else []
    return arr
