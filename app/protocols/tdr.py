"""最新 4096 字节 TDR 数据帧解析服务。

协议来源：用户提供的《数据帧.docx》。本模块只按最新协议解析：

    Byte0          帧头，固定 0xAA
    Byte1          当前相间编码，0x00~0x09
    Byte2          当前后端不解析
    Byte3~22       10 组相间故障距离，每组 uint16 大端序，单位 m
    Byte23~32      10 组相间故障类型
    Byte33~4032    4000 个波形采样点，每点 1 字节
    Byte4033~4034  预留区前两字节：设备端设置的监测电缆长度，uint16 大端序，单位 m
    Byte4035~4094  预留区剩余字段
    Byte4095       帧尾，固定 0x55

数据库 tdr_parsed_sample 的 ``phase`` 列是历史命名。为了兼容现有表结构，
该列现在保存相间名称（例如 A-B），不再保存旧协议中的单相 A/B/C/N。
"""

from typing import Any, Dict, Optional

from app.utils.hex_codec import compact_hex_text


# =========================
# 1. 最新数据帧固定布局
# =========================

# 完整帧固定 4096 字节，帧长不足或超出都视为协议错误。
TDR_TOTAL_BYTES = 4096
TDR_START_BYTE = 0xAA
TDR_END_BYTE = 0x55

# 波形从 Byte33 开始，到 Byte4032 结束，共 4000 字节。
# Python 切片右边界不包含，因此使用 raw[33:4033]。
TDR_WAVE_START = 33
TDR_WAVE_POINTS = 4000
TDR_WAVE_END = TDR_WAVE_START + TDR_WAVE_POINTS

# Byte4033~4094 是预留区；其中前两个字节已定义为设备端设置的监测电缆长度。
# Byte4095 是帧尾。
TDR_RESERVED_START = 4033
TDR_RESERVED_END = 4095

# 设备端设置的监测电缆长度位于预留字段前两个字节，采用 uint16 大端序，
# 即 Byte4033 * 256 + Byte4034；协议单位已经是 m，不再执行 km->m 换算。
TDR_DEVICE_CABLE_LENGTH_START = 4033
TDR_DEVICE_CABLE_LENGTH_END = 4035


# =========================
# 2. 相间编码及字段偏移
# =========================

# Byte1 的 10 种相间编码。顺序也决定 Byte3~22 和 Byte23~32 的字段顺序。
PHASE_PAIR_CODE_MAP = {
    0x00: "A-B",
    0x01: "A-C",
    0x02: "A-N",
    0x03: "A-E",
    0x04: "B-C",
    0x05: "B-N",
    0x06: "B-E",
    0x07: "C-N",
    0x08: "C-E",
    0x09: "N-E",
}
PHASE_PAIR_ORDER = tuple(PHASE_PAIR_CODE_MAP.values())
PHASE_PAIR_TO_CODE = {pair: code for code, pair in PHASE_PAIR_CODE_MAP.items()}

# Byte3~22：每个相间距离占 2 字节，采用大端序。
TDR_DISTANCE_OFFSET = {
    pair: 3 + index * 2 for index, pair in enumerate(PHASE_PAIR_ORDER)
}

# Byte23~32：每个相间对应 1 字节故障类型。
TDR_FAULT_TYPE_OFFSET = {
    pair: 23 + index for index, pair in enumerate(PHASE_PAIR_ORDER)
}

# 最新协议定义：0=正常，1=开路，2=短路。
DEVICE_FAULT_TYPE_MAP = {
    0x00: "NORMAL",
    0x01: "OPEN",
    0x02: "SHORT",
}


def _payload_hex_to_bytes(payload_hex: str) -> bytes:
    """把数据库中的十六进制 payload 转为 bytes，并检查基础格式。"""
    clean_hex = compact_hex_text(payload_hex)
    if not clean_hex:
        raise ValueError("payload hex is empty")
    if len(clean_hex) % 2 != 0:
        raise ValueError(f"payload hex length is odd: {len(clean_hex)}")
    try:
        return bytes.fromhex(clean_hex)
    except ValueError as exc:
        raise ValueError(f"payload is not valid hex: {exc}") from exc


def phase_pair_from_code(code: int) -> Optional[str]:
    """把 Byte1 的 0x00~0x09 转换为 A-B 等相间名称。"""
    try:
        return PHASE_PAIR_CODE_MAP.get(int(code))
    except (TypeError, ValueError):
        return None


def normalize_phase_pair(value: Optional[str]) -> str:
    """统一相间名称，兼容 ``A-B``、``AB``、``0x00`` 和十进制编码。"""
    raw = str(value or "").strip().upper().replace("相", "")
    compact = raw.replace("_", "-").replace("/", "-").replace(" ", "")

    if compact in PHASE_PAIR_TO_CODE:
        return compact

    if len(compact) == 2 and compact[0] in "ABCN" and compact[1] in "BCNE":
        candidate = f"{compact[0]}-{compact[1]}"
        if candidate in PHASE_PAIR_TO_CODE:
            return candidate

    try:
        code = int(compact, 16) if compact.startswith("0X") else int(compact)
        pair = phase_pair_from_code(code)
        if pair:
            return pair
    except (TypeError, ValueError):
        pass

    # 仅在外部 JSON 未传相间且帧头无法读取时使用默认值；正常帧始终以 Byte1 为准。
    return "A-B"


def parse_device_reported_cable_length_m(raw: bytes) -> Optional[float]:
    """读取预留区 Byte4033~4034 中设备端设置的监测电缆长度，单位为 m。

    Byte2 不参与电缆长度解析。这里按无符号 16 位大端序读取，也就是
    ``Byte4033 * 256 + Byte4034``。值为 0 时认为设备没有上报有效长度。
    """
    if len(raw) < TDR_DEVICE_CABLE_LENGTH_END:
        return None
    length_m = int.from_bytes(
        raw[TDR_DEVICE_CABLE_LENGTH_START:TDR_DEVICE_CABLE_LENGTH_END],
        byteorder="big",
        signed=False,
    )
    return float(length_m) if length_m > 0 else None


def _validate_frame(raw: bytes) -> None:
    """严格校验最新协议的帧长、帧头、帧尾和相间编码。"""
    if len(raw) != TDR_TOTAL_BYTES:
        raise ValueError(
            f"payload bytes invalid: {len(raw)}, expected exactly {TDR_TOTAL_BYTES}"
        )
    if raw[0] != TDR_START_BYTE:
        raise ValueError(
            f"invalid start byte: 0x{raw[0]:02X}, expected 0x{TDR_START_BYTE:02X}"
        )
    if raw[-1] != TDR_END_BYTE:
        raise ValueError(
            f"invalid end byte: 0x{raw[-1]:02X}, expected 0x{TDR_END_BYTE:02X}"
        )
    if phase_pair_from_code(raw[1]) is None:
        raise ValueError(f"invalid phase-pair code: 0x{raw[1]:02X}, expected 0x00~0x09")


def get_frame_basic_info(payload_hex: str) -> Dict[str, Any]:
    """返回帧头、帧尾、当前相间和设备上报电缆长度。"""
    raw = _payload_hex_to_bytes(payload_hex)
    phase_pair_code = int(raw[1]) if len(raw) > 1 else None
    phase_pair = phase_pair_from_code(phase_pair_code) if phase_pair_code is not None else None
    reported_length_m = parse_device_reported_cable_length_m(raw)

    return {
        "start_byte": int(raw[0]) if raw else None,
        "end_byte": int(raw[-1]) if raw else None,
        "valid_start": bool(raw and raw[0] == TDR_START_BYTE),
        "valid_end": bool(raw and raw[-1] == TDR_END_BYTE),
        "valid_length": len(raw) == TDR_TOTAL_BYTES,
        "phase_pair_code": phase_pair_code,
        "phase_pair": phase_pair,
        # 兼容旧接口字段名；字段值现在是 A-B 等相间，而不是 A/B/C/N 单相。
        "phase_code": phase_pair_code,
        "phase": phase_pair,
        "device_reported_cable_length_m": reported_length_m,
        "cable_length_m_from_frame": reported_length_m,
        "payload_byte_len": len(raw),
    }


def parse_device_result_all(raw: bytes) -> Dict[str, Dict[str, Any]]:
    """解析 Byte3~32 中全部 10 组相间故障结果。

    距离按 ``高字节×256+低字节`` 解析。距离值为 0 时视为设备未给出
    有效故障距离，因此返回 ``None``；原始值仍保存在 ``distance_raw`` 中。
    """
    result: Dict[str, Dict[str, Any]] = {}
    for pair in PHASE_PAIR_ORDER:
        distance_offset = TDR_DISTANCE_OFFSET[pair]
        distance_value = int.from_bytes(
            raw[distance_offset:distance_offset + 2],
            byteorder="big",
            signed=False,
        )
        fault_code = int(raw[TDR_FAULT_TYPE_OFFSET[pair]])
        result[pair] = {
            "type": DEVICE_FAULT_TYPE_MAP.get(fault_code, f"CODE_{fault_code}"),
            "code": fault_code,
            "distance_m": float(distance_value) if distance_value > 0 else None,
            "distance_raw": distance_value,
        }
    return result


def parse_tdr_payload_hex(payload_hex: str, phase_pair: Optional[str] = None) -> Dict[str, Any]:
    """严格按最新协议解析一条 4096 字节数据帧。

    ``phase_pair`` 仅作为外部提示保留；有效帧始终以 Byte1 为权威来源，
    防止 EMQX JSON 中的附加字段与设备原始帧不一致。
    """
    raw = _payload_hex_to_bytes(payload_hex)
    _validate_frame(raw)

    pair_code = int(raw[1])
    frame_phase_pair = phase_pair_from_code(pair_code)
    if frame_phase_pair is None:  # _validate_frame 已拦截，仅用于类型收窄。
        raise ValueError(f"unsupported phase-pair code: {pair_code}")

    # Byte33~4032 每点 1 字节，数据库与 API 保留原始 0~255 数值。
    # 前端绘图时再按 uint8 -> int8 转换，以显示 -128~127 的正负波动。
    waveform = list(raw[TDR_WAVE_START:TDR_WAVE_END])
    if len(waveform) != TDR_WAVE_POINTS:
        raise ValueError(f"waveform points invalid: {len(waveform)}")

    device_result_all = parse_device_result_all(raw)
    current_device_result = device_result_all[frame_phase_pair]
    reported_length_m = parse_device_reported_cable_length_m(raw)

    return {
        "phase_pair": frame_phase_pair,
        "phase_pair_code": pair_code,
        # 兼容既有数据库/API 字段名；值已改为相间名称。
        "phase": frame_phase_pair,
        "frame_phase": frame_phase_pair,
        "phase_code": pair_code,
        "waveform_4000": waveform,
        "device_fault_type": current_device_result["type"],
        "device_fault_distance_m": current_device_result["distance_m"],
        "device_result_all": device_result_all,
        "device_reported_cable_length_m": reported_length_m,
        "cable_length_m_from_frame": reported_length_m,
        "payload_byte_len": len(raw),
        "start_byte": int(raw[0]),
        "end_byte": int(raw[-1]),
        "reserved_bytes": len(raw[TDR_RESERVED_START:TDR_RESERVED_END]),
    }
