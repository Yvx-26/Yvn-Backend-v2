"""大屏设备、波形、定位及汇总数据接口。"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from app.db.mysql import get_conn
from app.services.waveform import payload_to_samples, extract_waveform_from_samples
from app.services.tdr.parser import PHASE_PAIR_ORDER, parse_tdr_payload_hex

router = APIRouter()


# 接口 GET /api/dashboard/summary：统计设备总数、今日波形数和定位记录数，返回大屏顶部汇总指标。
@router.get('/api/dashboard/summary')
def api_dashboard_summary():
    """返回大屏顶部使用的设备、波形、定位和告警汇总数。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(DISTINCT IMEI) AS device_total FROM DEVICE_RAW_FRAME")
        r1 = cur.fetchone() or {}

        cur.execute("SELECT COUNT(*) AS wave_today FROM DEVICE_RAW_FRAME WHERE DATE(CREATED_AT)=CURDATE()")
        r2 = cur.fetchone() or {}

        cur.execute("SELECT COUNT(*) AS location_today FROM device_location_latest")
        r3 = cur.fetchone() or {}

        return {
            "device_total": int(r1.get("device_total") or 0),
            "wave_today": int(r2.get("wave_today") or 0),
            "location_today": int(r3.get("location_today") or 0),
            # 当前系统尚无独立告警表，保留字段以兼容现有前端契约。
            "alarm_count": 0,
        }
    finally:
        cur.close()
        conn.close()


# 接口 GET /api/dashboard/devices：查询设备绑定信息及各设备最近一次上报时间，形成设备状态列表。
@router.get('/api/dashboard/devices')
def api_dashboard_devices():
    """返回设备最新上报时间、定位和电缆绑定信息。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        sql = """
        SELECT
            t.IMEI AS imei,
            b.cable_key AS cable_key,
            COALESCE(c.cable_name, b.cable_key) AS cable_name,
            t.CREATED_AT AS last_wave_time,
            TIMESTAMPDIFF(SECOND, t.CREATED_AT, NOW()) AS age_seconds
        FROM (
            SELECT IMEI, MAX(CREATED_AT) AS CREATED_AT
            FROM DEVICE_RAW_FRAME
            GROUP BY IMEI
        ) t
        LEFT JOIN wave_device_bind b ON b.imei = t.IMEI
        LEFT JOIN cable_info c ON c.cable_key = b.cable_key
        ORDER BY t.CREATED_AT DESC
        """
        cur.execute(sql)
        rows = cur.fetchall() or []

        out = []
        for row in rows:
            age_seconds = row.get("age_seconds")
            out.append({
                "imei": row.get("imei"),
                "cable_key": row.get("cable_key"),
                "cable_name": row.get("cable_name"),
                "last_wave_time": str(row.get("last_wave_time")) if row.get("last_wave_time") else None,
                "online": age_seconds is not None and int(age_seconds) <= 90,
                "age_seconds": int(age_seconds) if age_seconds is not None else None,
            })
        return out
    finally:
        cur.close()
        conn.close()


# 接口 GET /api/dashboard/wave/latest：读取指定设备最新一帧波形，并转换为前端大屏需要的采样数组。
@router.get('/api/dashboard/wave/latest')
def api_dashboard_wave_latest(
    imei: str,
    phase_pair: Optional[str] = Query(None),
):
    """返回指定设备最新一帧，或指定相间最近一帧的 4000 点有效波形。"""
    selected_phase = phase_pair.strip().upper() if phase_pair is not None else None
    if selected_phase is not None and selected_phase not in PHASE_PAIR_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"invalid phase_pair: {phase_pair}",
        )

    conn = get_conn()
    cur = conn.cursor()
    try:
        if selected_phase is None:
            cur.execute("""
                SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
                FROM DEVICE_RAW_FRAME
                WHERE IMEI=%s
                ORDER BY CREATED_AT DESC, ID DESC
                LIMIT 1
            """, (imei,))
        else:
            # DEVICE_RAW_FRAME.CABLE 已在数据入库时按 Byte1 保存为标准相间名称。
            # 这里按相间筛选，使前端点击十个卡片时能读取各自最近一帧波形。
            cur.execute("""
                SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
                FROM DEVICE_RAW_FRAME
                WHERE IMEI=%s AND CABLE=%s
                ORDER BY CREATED_AT DESC, ID DESC
                LIMIT 1
            """, (imei, selected_phase))
        row = cur.fetchone()

        if not row:
            return {
                "imei": imei,
                "samples": [],
                "waveform_samples": [],
                "points": 0,
                "waveform_points": 0,
                "min": 0,
                "max": 0,
                "frame_id": None,
                "frame_valid": False,
                "phase_pair": selected_phase,
                "interphase_results": {},
            }

        # samples 保留完整 4096 字节帧，便于协议排查；前端业务字段不再自行按偏移解析。
        samples = payload_to_samples(row.get("PAYLOAD"))

        # 最新协议必须同时校验帧长、0xAA 帧头、0x55 帧尾和相间编码。
        # 解析成功后，波形及 10 组相间故障结果统一由后端返回，避免多个前端重复实现协议。
        parsed = None
        frame_error = None
        try:
            parsed = parse_tdr_payload_hex(str(row.get("PAYLOAD") or ""))
            waveform_samples = parsed["waveform_4000"]
        except Exception as exc:
            waveform_samples = extract_waveform_from_samples(samples)
            frame_error = str(exc)

        # min/max 优先按有效波形计算，避免帧头和预留字段影响波形统计。
        stat_samples = waveform_samples or samples
        mn = min(stat_samples) if stat_samples else 0
        mx = max(stat_samples) if stat_samples else 0

        return {
            "frame_id": row.get("ID"),
            "imei": row.get("IMEI"),
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
            "created_at": str(row.get("CREATED_AT")) if row.get("CREATED_AT") else None,
            "points": len(samples),
            "waveform_points": len(waveform_samples),
            "min": mn,
            "max": mx,
            "samples": samples,
            "waveform_samples": waveform_samples,
        }
    finally:
        cur.close()
        conn.close()


# 接口 GET /api/dashboard/map：联查设备绑定和最近定位信息，返回地图标记点数据。
@router.get('/api/dashboard/map')
def api_dashboard_map():
    """返回地图绘制所需的设备位置、电缆绑定和显示名称。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                d.device_id AS imei,
                d.lng AS lng,
                d.lat AS lat,
                d.ts AS ts,
                d.updated_at AS updated_at,
                b.cable_key AS cable_key,
                COALESCE(c.cable_name, b.cable_key) AS cable_name
            FROM device_location_latest d
            LEFT JOIN wave_device_bind b ON b.imei = d.device_id
            LEFT JOIN cable_info c ON c.cable_key = b.cable_key
            WHERE d.lng IS NOT NULL AND d.lat IS NOT NULL
            ORDER BY d.updated_at DESC
        """)
        rows = cur.fetchall() or []

        items = []
        for row in rows:
            lng = row.get('lng') if isinstance(row, dict) else row[1]
            lat = row.get('lat') if isinstance(row, dict) else row[2]
            ts = row.get('ts') if isinstance(row, dict) else row[3]
            updated_at = row.get('updated_at') if isinstance(row, dict) else row[4]

            items.append({
                'imei': row.get('imei') if isinstance(row, dict) else row[0],
                'lng': float(lng) if lng is not None else None,
                'lat': float(lat) if lat is not None else None,
                'ts': int(ts) if ts is not None else None,
                'updated_at': str(updated_at) if updated_at is not None else None,
                'cable_key': row.get('cable_key') if isinstance(row, dict) else row[5],
                'cable_name': row.get('cable_name') if isinstance(row, dict) else row[6],
            })
        return items
    finally:
        cur.close()
        conn.close()


@router.get('/api/dashboard/operation')
def api_dashboard_operation():
    """返回由真实原始帧汇总出的今日运行态势。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   MIN(CREATED_AT) AS first_time,
                   MAX(CREATED_AT) AS last_time
            FROM DEVICE_RAW_FRAME
            WHERE DATE(CREATED_AT)=CURDATE()
            """
        )
        totals = cur.fetchone() or {}
        total = int(totals.get("total") or 0)
        first_time = totals.get("first_time")
        last_time = totals.get("last_time")
        average_interval = 0.0
        if total > 1 and first_time and last_time:
            average_interval = max(0.0, (last_time - first_time).total_seconds() / (total - 1))

        cur.execute(
            """
            SELECT FLOOR(HOUR(CREATED_AT) / 2) * 2 AS bucket_hour, COUNT(*) AS frame_count
            FROM DEVICE_RAW_FRAME
            WHERE DATE(CREATED_AT)=CURDATE()
            GROUP BY bucket_hour
            ORDER BY bucket_hour
            """
        )
        hourly = {int(row["bucket_hour"]): int(row["frame_count"]) for row in (cur.fetchall() or [])}

        cur.execute(
            """
            SELECT COALESCE(c.cable_name, b.cable_key, '未绑定线路') AS line_name,
                   COALESCE(b.cable_key, 'unbound') AS line_id,
                   COUNT(*) AS frame_count
            FROM DEVICE_RAW_FRAME r
            LEFT JOIN wave_device_bind b ON b.imei = r.IMEI
            LEFT JOIN cable_info c ON c.cable_key = b.cable_key
            WHERE DATE(r.CREATED_AT)=CURDATE()
            GROUP BY line_id, line_name
            ORDER BY frame_count DESC
            """
        )
        line_rows = cur.fetchall() or []

        cur.execute(
            """
            SELECT ID, IMEI, CABLE, CREATED_AT
            FROM DEVICE_RAW_FRAME
            ORDER BY CREATED_AT DESC, ID DESC
            LIMIT 8
            """
        )
        record_rows = cur.fetchall() or []

        cur.execute(
            """
            SELECT COUNT(*) AS device_total,
                   SUM(CASE WHEN age_seconds <= 90 THEN 1 ELSE 0 END) AS online_total
            FROM (
                SELECT TIMESTAMPDIFF(SECOND, MAX(CREATED_AT), NOW()) AS age_seconds
                FROM DEVICE_RAW_FRAME
                GROUP BY IMEI
            ) latest
            """
        )
        device_counts = cur.fetchone() or {}
        device_total = int(device_counts.get("device_total") or 0)
        online_total = int(device_counts.get("online_total") or 0)
        online_rate = round((online_total / device_total) * 100, 1) if device_total else 0.0

        return {
            "wave_today": total,
            "average_interval_seconds": round(average_interval, 1),
            "online_devices": online_total,
            "device_total": device_total,
            "online_rate": online_rate,
            "receive_trend": [
                {"label": f"{hour:02d}:00", "value": hourly.get(hour, 0)}
                for hour in range(0, 24, 2)
            ],
            "line_volumes": [
                {
                    "id": row.get("line_id"),
                    "name": row.get("line_name"),
                    "value": int(row.get("frame_count") or 0),
                }
                for row in line_rows
            ],
            "records": [
                {
                    "id": str(row.get("ID")),
                    "imei": row.get("IMEI"),
                    "phase": row.get("CABLE"),
                    "created_at": str(row.get("CREATED_AT")) if row.get("CREATED_AT") else None,
                }
                for row in record_rows
            ],
        }
    finally:
        cur.close()
        conn.close()
