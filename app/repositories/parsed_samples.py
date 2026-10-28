"""解析样本持久化；协议规则只来自 app.protocols.tdr。"""

import json
from typing import Any, Dict, Optional

from app.core.config import TABLE_TDR_PARSED_SAMPLE
from app.protocols.tdr import normalize_phase_pair, parse_tdr_payload_hex


def build_sample_name(raw_frame_id: int, phase_pair: str) -> str:
    """按原始帧编号和相间名称生成稳定的自动解析样本名。"""
    normalized_pair = normalize_phase_pair(phase_pair)
    safe_pair = normalized_pair.replace("-", "_")
    return f"LVTD_RF{int(raw_frame_id):06d}_{safe_pair}"


def insert_tdr_parsed_sample(
    cursor,
    raw_frame_id: int,
    imei: str,
    phase_pair: str,
    payload_hex: str,
    payload_len: int,
    frame_created_at=None,
    cable_key: Optional[str] = None,
    cable_length_m: Optional[float] = None,
    device_reported_cable_length_m: Optional[float] = None,
) -> Dict[str, Any]:
    """解析最新数据帧并写入 ``tdr_parsed_sample``。

    表中的 ``phase`` 列沿用历史列名，但现在保存 A-B 等相间名称。
    ``device_result_all`` 保存 10 组真实距离和故障类型，便于前端直接展示。
    """
    parsed = parse_tdr_payload_hex(payload_hex=payload_hex, phase_pair=phase_pair)
    frame_phase_pair = parsed["phase_pair"]

    if device_reported_cable_length_m is None:
        device_reported_cable_length_m = parsed["device_reported_cable_length_m"]

    sample_name = build_sample_name(raw_frame_id, frame_phase_pair)
    waveform_json = json.dumps(parsed["waveform_4000"], ensure_ascii=False)

    sql = f"""
    INSERT INTO {TABLE_TDR_PARSED_SAMPLE}
    (
        sample_name, raw_frame_id, device_imei, cable_key, phase,
        waveform_4000, device_fault_type, device_fault_distance_m,
        device_result_all, cable_length_m, device_reported_cable_length_m,
        payload_len, frame_created_at, parse_status, sample_status, parse_message
    )
    VALUES
    (
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s, 'success', 'unlabeled', %s
    )
    ON DUPLICATE KEY UPDATE
        raw_frame_id = VALUES(raw_frame_id),
        device_imei = VALUES(device_imei),
        cable_key = VALUES(cable_key),
        phase = VALUES(phase),
        waveform_4000 = VALUES(waveform_4000),
        device_fault_type = VALUES(device_fault_type),
        device_fault_distance_m = VALUES(device_fault_distance_m),
        device_result_all = VALUES(device_result_all),
        cable_length_m = VALUES(cable_length_m),
        device_reported_cable_length_m = VALUES(device_reported_cable_length_m),
        payload_len = VALUES(payload_len),
        frame_created_at = VALUES(frame_created_at),
        parse_status = 'success',
        parse_message = VALUES(parse_message),
        updated_at = CURRENT_TIMESTAMP
    """

    parse_message = f"auto parsed latest protocol from raw_frame_id={raw_frame_id}"
    cursor.execute(
        sql,
        (
            sample_name,
            int(raw_frame_id),
            str(imei),
            cable_key,
            frame_phase_pair,
            waveform_json,
            parsed["device_fault_type"],
            parsed["device_fault_distance_m"],
            json.dumps(parsed["device_result_all"], ensure_ascii=False),
            cable_length_m,
            device_reported_cable_length_m,
            int(payload_len),
            frame_created_at,
            parse_message,
        ),
    )

    return {
        "sample_name": sample_name,
        "phase_pair": frame_phase_pair,
        "phase_pair_code": parsed["phase_pair_code"],
        # 兼容旧响应字段。
        "phase": frame_phase_pair,
        "phase_code": parsed["phase_pair_code"],
        "frame_phase": frame_phase_pair,
        "points": len(parsed["waveform_4000"]),
        "device_fault_type": parsed["device_fault_type"],
        "device_fault_distance_m": parsed["device_fault_distance_m"],
        "device_result_all": parsed["device_result_all"],
        "device_reported_cable_length_m": device_reported_cable_length_m,
        "cable_length_m_from_frame": parsed["cable_length_m_from_frame"],
        "payload_byte_len": parsed["payload_byte_len"],
        "start_byte": parsed["start_byte"],
        "end_byte": parsed["end_byte"],
    }
