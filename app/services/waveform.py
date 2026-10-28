"""公共波形业务：统一解码，按原接口投影响应；不负责 HTTP 或事务提交。"""

from app.protocols.tdr import parse_tdr_payload_hex
from app.repositories import waveforms
from app.services.compat.waveform import payload_to_samples, extract_waveform_from_samples


def decode_frame(row, max_points=None):
    samples = payload_to_samples(row.get("PAYLOAD"))
    if max_points is not None and len(samples) > max_points:
        samples = samples[:max_points]
    parsed, frame_error = None, None
    try:
        parsed = parse_tdr_payload_hex(str(row.get("PAYLOAD") or ""))
        waveform_samples = parsed["waveform_4000"]
    except Exception as exc:
        waveform_samples = extract_waveform_from_samples(samples)
        frame_error = str(exc)
    return {
        "phase_pair": parsed.get("phase_pair") if parsed else None,
        "phase_pair_code": parsed.get("phase_pair_code") if parsed else None,
        "fault_type": parsed.get("device_fault_type") if parsed else None,
        "fault_distance_m": parsed.get("device_fault_distance_m") if parsed else None,
        "interphase_results": parsed.get("device_result_all") if parsed else {},
        "device_reported_cable_length_m": parsed.get("device_reported_cable_length_m") if parsed else None,
        "frame_valid": parsed is not None,
        "frame_error": frame_error,
        "points": len(samples),
        "waveform_points": len(waveform_samples),
        "samples": samples,
        "waveform_samples": waveform_samples,
    }


def _empty_frame(imei, phase_pair=None):
    return {
        "imei": imei, "frame_id": None, "samples": [], "waveform_samples": [],
        "points": 0, "waveform_points": 0, "frame_valid": False,
        "phase_pair": phase_pair, "interphase_results": {},
    }


def latest(conn, imei, phase_pair):
    with conn.cursor() as cursor:
        row = waveforms.find_latest_frame(cursor, imei, phase_pair)
    if not row:
        return {**_empty_frame(imei, phase_pair), "min": 0, "max": 0}
    decoded = decode_frame(row)
    stats = decoded["waveform_samples"] or decoded["samples"]
    return {
        **decoded, "frame_id": row.get("ID"), "imei": row.get("IMEI"), "cable": row.get("CABLE"),
        "created_at": str(row.get("CREATED_AT")) if row.get("CREATED_AT") else None,
        "min": min(stats) if stats else 0, "max": max(stats) if stats else 0,
    }


def history(conn, imei, frame_id, min_hex_len, max_points):
    with conn.cursor() as cursor:
        row = waveforms.find_history_frame(cursor, imei, frame_id, min_hex_len)
    if not row:
        return {**_empty_frame(imei), "dt_ns": 10}
    return {
        **decode_frame(row, max_points), "imei": imei, "frame_id": row["ID"],
        "cable": row.get("CABLE"), "dt_ns": 10, "payload_len": row["PAYLOAD_LEN"],
        "created_at": str(row.get("CREATED_AT")) if row.get("CREATED_AT") else None,
    }


def list_device_ids(conn, limit):
    with conn.cursor() as cursor:
        rows = waveforms.list_device_ids(cursor, limit)
    return {"devices": [row["IMEI"] for row in rows]}


def list_frames(conn, imei, limit, before_id):
    with conn.cursor() as cursor:
        rows = waveforms.list_frame_rows(cursor, imei, limit, before_id)
    return {"data": rows, "next_before_id": rows[-1]["ID"] if rows else None}
