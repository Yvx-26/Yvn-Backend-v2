"""EMQX payload 与 topic 处理工具。"""

import base64
import binascii
import re
from typing import Optional, Dict, Any
from app.core.config import RAW_MIN_LEN


class _AllowedLens:
    """判断 payload 长度是否允许写入原始波形表。"""

    def __contains__(self, value):
        try:
            return int(value) >= RAW_MIN_LEN
        except Exception:
            return False


# 允许直接写入原始帧表的典型 payload 长度集合。
RAW_ALLOWED_LENS = _AllowedLens()
# 用于识别纯十六进制文本的正则表达式。
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _compact_text(text: str) -> str:
    return (
        str(text or "")
        .strip()
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("\t", "")
    )


def _is_hex_string(text: str) -> bool:
    """判断字符串是否像十六进制 payload。"""
    s = _compact_text(text)
    return bool(s) and len(s) % 2 == 0 and _HEX_RE.match(s) is not None


def normalize_payload_to_hex(raw_payload: Optional[str], encoding: Optional[str] = None) -> Dict[str, Any]:
    """
    把 EMQX 转发来的 payload 统一转成十六进制字符串。

    推荐链路：
        MQTT 二进制 payload -> EMQX base64_encode(payload) AS payload_b64
        -> HTTP JSON 中传 payload_b64 -> 后端 Base64 解码 -> 保存为 hex。

    兼容字段：
        encoding='base64'：raw_payload 是 Base64 字符串；
        encoding='hex'：raw_payload 是十六进制字符串；
        encoding=None：自动识别，优先识别 hex，再尝试 base64。

    返回：
        hex: 十六进制字符串；
        length: 十六进制字符长度。4096 字节对应 8192。
    """
    if raw_payload is None:
        return {"hex": "", "length": 0, "byte_length": 0, "encoding": encoding or "missing"}

    if isinstance(raw_payload, (bytes, bytearray)):
        payload_hex = bytes(raw_payload).hex()
        return {
            "hex": payload_hex,
            "length": len(payload_hex),
            "byte_length": len(raw_payload),
            "encoding": "binary",
        }

    text = str(raw_payload).strip()
    fmt = str(encoding or "auto").lower().strip()

    try:
        if fmt in ("hex", "payload_hex"):
            clean = _compact_text(text)
            bytes.fromhex(clean)
            payload_hex = clean.lower()
            return {
                "hex": payload_hex,
                "length": len(payload_hex),
                "byte_length": len(payload_hex) // 2,
                "encoding": "hex",
            }

        if fmt in ("base64", "b64", "payload_b64"):
            decoded = base64.b64decode(text, validate=True)
            payload_hex = decoded.hex()
            return {
                "hex": payload_hex,
                "length": len(payload_hex),
                "byte_length": len(decoded),
                "encoding": "base64",
            }

        # 自动识别时必须先判断 hex，再判断 base64。
        # 4096 字节 hex 是 8192 个十六进制字符；如果先按 base64 解码，可能误解码成错误长度。
        if _is_hex_string(text):
            clean = _compact_text(text)
            payload_hex = clean.lower()
            return {
                "hex": payload_hex,
                "length": len(payload_hex),
                "byte_length": len(payload_hex) // 2,
                "encoding": "hex",
            }

        decoded = base64.b64decode(text, validate=True)
        payload_hex = decoded.hex()
        return {
            "hex": payload_hex,
            "length": len(payload_hex),
            "byte_length": len(decoded),
            "encoding": "base64",
        }

    except (binascii.Error, ValueError):
        # 最后兜底：作为普通字符串保存其 UTF-8 字节，方便排查错误消息。
        raw_bytes = text.encode("utf-8", errors="replace")
        payload_hex = raw_bytes.hex()
        return {
            "hex": payload_hex,
            "length": len(payload_hex),
            "byte_length": len(raw_bytes),
            "encoding": "utf8_fallback",
        }
