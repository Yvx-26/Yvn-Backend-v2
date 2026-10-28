"""大屏统计与显示状态组装；不接收 HTTP 请求，不执行 SQL。"""

from app.repositories import dashboard as queries


def summary(conn):
    """返回大屏顶部使用的设备、波形、定位和告警汇总数。"""
    with conn.cursor() as cur:
        r1, r2, r3 = queries.summary(cur)

        return {
            "device_total": int(r1.get("device_total") or 0),
            "wave_today": int(r2.get("wave_today") or 0),
            "location_today": int(r3.get("location_today") or 0),
            # 当前系统尚无独立告警表，保留字段以兼容现有前端契约。
            "alarm_count": 0,
        }


def devices(conn):
    """返回设备最新上报时间、定位和电缆绑定信息。"""
    with conn.cursor() as cur:
        rows = queries.device_rows(cur)

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


def map_points(conn):
    """返回地图绘制所需的设备位置、电缆绑定和显示名称。"""
    with conn.cursor() as cur:
        rows = queries.map_rows(cur)

        items = []
        for row in rows:
            lng = row.get('lng') if isinstance(row, dict) else row[1]
            lat = row.get('lat') if isinstance(row, dict) else row[2]
            ts = row.get('ts') if isinstance(row, dict) else row[3]
            updated_at = row.get('updated_at') if isinstance(row, dict) else row[4]
            age_seconds = row.get('age_seconds') if isinstance(row, dict) else row[7]

            items.append({
                'imei': row.get('imei') if isinstance(row, dict) else row[0],
                'lng': float(lng) if lng is not None else None,
                'lat': float(lat) if lat is not None else None,
                'ts': int(ts) if ts is not None else None,
                'updated_at': str(updated_at) if updated_at is not None else None,
                'cable_key': row.get('cable_key') if isinstance(row, dict) else row[5],
                'cable_name': row.get('cable_name') if isinstance(row, dict) else row[6],
                'online': age_seconds is not None and int(age_seconds) <= 90,
                'age_seconds': int(age_seconds) if age_seconds is not None else None,
            })
        return items


def operation(conn):
    """返回由真实原始帧汇总出的今日运行态势。"""
    with conn.cursor() as cur:
        totals = queries.operation_totals(cur)
        total = int(totals.get("total") or 0)
        first_time = totals.get("first_time")
        last_time = totals.get("last_time")
        average_interval = 0.0
        if total > 1 and first_time and last_time:
            average_interval = max(0.0, (last_time - first_time).total_seconds() / (total - 1))

        hourly = {int(row["bucket_hour"]): int(row["frame_count"]) for row in queries.receive_trend_rows(cur)}

        line_rows = queries.line_volume_rows(cur)

        record_rows = queries.recent_record_rows(cur)

        device_counts = queries.device_counts(cur)
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
