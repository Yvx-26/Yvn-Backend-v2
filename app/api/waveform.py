"""原始帧列表与波形查询接口。"""

from typing import Optional
from fastapi import APIRouter, Query, HTTPException

from app.core.config import TABLE_RAW, DEFAULT_MIN_HEX_LEN
from app.db.mysql import get_conn
from app.services.waveform import (
    payload_to_samples,
    extract_waveform_from_samples,
)
from app.services.tdr.parser import parse_tdr_payload_hex

router = APIRouter()


# 接口 GET /api/devices：查询原始帧表中出现过的设备 IMEI 列表。
@router.get("/api/devices")
def list_devices(limit: int = Query(200, ge=1, le=2000)):
    """按最近帧顺序返回原始帧表中出现过的设备 IMEI。"""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cursor:
            sql = f"""
            SELECT IMEI, MAX(ID) AS last_id
            FROM {TABLE_RAW}
            WHERE IMEI IS NOT NULL
              AND IMEI NOT IN ('None', 'sub_client', 'test_client')
            GROUP BY IMEI
            ORDER BY last_id DESC
            LIMIT %s
            """
            cursor.execute(sql, (limit,))
            rows = cursor.fetchall()
        return {"devices": [r["IMEI"] for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 接口 GET /api/frames：按设备筛选并分页返回可用原始帧摘要。
@router.get("/api/frames")
def list_frames(
    imei: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
    before_id: Optional[int] = Query(None),
):
    """分页返回指定设备的历史原始帧摘要。"""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cursor:
            if before_id is not None:
                sql = f"""
                SELECT ID, IMEI, CABLE, PAYLOAD_LEN, CREATED_AT
                FROM {TABLE_RAW}
                WHERE IMEI=%s AND ID < %s
                ORDER BY ID DESC
                LIMIT %s
                """
                cursor.execute(sql, (imei, before_id, limit))
            else:
                sql = f"""
                SELECT ID, IMEI, CABLE, PAYLOAD_LEN, CREATED_AT
                FROM {TABLE_RAW}
                WHERE IMEI=%s
                ORDER BY ID DESC
                LIMIT %s
                """
                cursor.execute(sql, (imei, limit))
            rows = cursor.fetchall()

        next_before_id = rows[-1]["ID"] if rows else None
        return {"data": rows, "next_before_id": next_before_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 接口 GET /api/waveform：读取指定原始帧，解析完整采样数组和 4000 点波形后返回前端。
@router.get("/api/waveform")
def waveform(
    imei: str = Query(...),
    frame_id: Optional[int] = Query(None),
    min_hex_len: int = Query(DEFAULT_MIN_HEX_LEN, ge=0, le=500000),
    max_points: int = Query(200000, ge=100, le=500000),
):
    """返回指定帧或最新有效帧，并同时提供完整包与 4000 点波形。"""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cursor:
            if frame_id is not None:
                # 这里直接写 IMEI 字段，不能再用 .replace("IMI", "IMEI")。
                # 否则 LIMIT 中的 IMI 会被替换成 IMEI，运行时变成 LIMEIT。
                sql = f"""
                SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
                FROM {TABLE_RAW}
                WHERE IMEI=%s AND ID=%s
                LIMIT 1
                """
                cursor.execute(sql, (imei, frame_id))
            else:
                sql = f"""
                SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
                FROM {TABLE_RAW}
                WHERE IMEI=%s AND PAYLOAD_LEN >= %s
                ORDER BY ID DESC
                LIMIT 1
                """
                cursor.execute(sql, (imei, min_hex_len))
            row = cursor.fetchone()

        if not row:
            return {
                "imei": imei,
                "frame_id": None,
                "dt_ns": 10,
                "points": 0,
                "waveform_points": 0,
                "samples": [],
                "waveform_samples": [],
                "frame_valid": False,
                "phase_pair": None,
                "interphase_results": {},
            }

        # samples 保留完整帧供调试使用；前端不再自行读取协议字节偏移。
        samples = payload_to_samples(row.get("PAYLOAD"))

        if len(samples) > max_points:
            samples = samples[:max_points]

        # 最新协议由后端统一严格解析：Byte33~4032 为波形，Byte3~32 为
        # 10 组相间故障结果。无效帧返回 frame_error，不把错误数据画成波形。
        parsed = None
        frame_error = None
        try:
            parsed = parse_tdr_payload_hex(str(row.get("PAYLOAD") or ""))
            waveform_samples = parsed["waveform_4000"]
        except Exception as exc:
            waveform_samples = extract_waveform_from_samples(samples)
            frame_error = str(exc)

        return {
            "imei": imei,
            "frame_id": row["ID"],
            "cable": row.get("CABLE"),
            "phase_pair": parsed.get("phase_pair") if parsed else None,
            "phase_pair_code": parsed.get("phase_pair_code") if parsed else None,
            "fault_type": parsed.get("device_fault_type") if parsed else None,
            "fault_distance_m": parsed.get("device_fault_distance_m") if parsed else None,
            "interphase_results": parsed.get("device_result_all") if parsed else {},
            "device_reported_cable_length_m": (
                parsed.get("device_reported_cable_length_m") if parsed else None
            ),
            "frame_valid": parsed is not None,
            "frame_error": frame_error,
            "dt_ns": 10,
            "points": len(samples),
            "waveform_points": len(waveform_samples),
            "payload_len": row["PAYLOAD_LEN"],
            "created_at": str(row.get("CREATED_AT")) if row.get("CREATED_AT") else None,
            "samples": samples,
            "waveform_samples": waveform_samples,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()
