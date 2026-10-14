"""线路与设备资料的 MySQL 持久化服务。"""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.schemas.infrastructure import ManagedDevice, ManagedLine


def _json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")


def _dump_payload(model: ManagedDevice | ManagedLine) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _load_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str) and raw:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    return {}


def _timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _device_from_row(row: dict[str, Any]) -> dict[str, Any]:
    data = _load_payload(row.get("payload_json"))
    acquisition = data.get("acquisition")
    if isinstance(acquisition, dict):
        # 兼容早期前端把一帧误建模为“10 条相间批次”的已保存配置。
        if acquisition.get("batchSize") == 10:
            acquisition["batchSize"] = 4000
        if acquisition.get("triggerThreshold") == "2.50 V":
            acquisition["triggerThreshold"] = ""
    data.update(
        {
            "id": row.get("device_id") or "",
            "lineId": row.get("line_id") or "",
            "category": row.get("category") or "integrated",
            "state": row.get("state") or "offline",
            "enabled": bool(row.get("enabled")),
            "updatedAt": _timestamp(row.get("updated_at")),
        }
    )
    return data


def list_devices(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT device_id, line_id, category, state, enabled, payload_json, updated_at
            FROM managed_device
            ORDER BY device_id
            """
        )
        rows = cursor.fetchall() or []
    return [_device_from_row(row) for row in rows]


def list_lines(conn, devices: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    device_items = devices if devices is not None else list_devices(conn)
    device_by_id = {item["id"]: item for item in device_items}

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT line_id, name, line_type, length_km, description, state,
                   payload_json, updated_at
            FROM managed_line
            ORDER BY line_id
            """
        )
        line_rows = cursor.fetchall() or []
        cursor.execute(
            """
            SELECT line_id, device_id, sequence_no
            FROM managed_line_device
            ORDER BY line_id, sequence_no, device_id
            """
        )
        binding_rows = cursor.fetchall() or []

    bindings: dict[str, list[dict[str, Any]]] = {}
    for binding in binding_rows:
        device = device_by_id.get(binding.get("device_id"))
        if not device:
            continue
        bindings.setdefault(binding["line_id"], []).append(
            {**device, "sequence": int(binding.get("sequence_no") or 0)}
        )

    result = []
    for row in line_rows:
        data = _load_payload(row.get("payload_json"))
        data.update(
            {
                "id": row.get("line_id") or "",
                "name": row.get("name") or "",
                "type": row.get("line_type") or "land",
                "lengthKm": str(row.get("length_km") or ""),
                "description": row.get("description") or "",
                "state": row.get("state") or "draft",
                "updatedAt": _timestamp(row.get("updated_at")),
                "devices": bindings.get(row.get("line_id"), []),
            }
        )
        result.append(data)
    return result


def _runtime_device_inventory(conn) -> list[dict[str, Any]]:
    """从真实采集表发现设备，不要求先在 managed_device 中人工建档。"""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT IMEI AS device_id, MAX(CREATED_AT) AS last_wave_time
            FROM DEVICE_RAW_FRAME
            WHERE IMEI IS NOT NULL AND IMEI <> ''
            GROUP BY IMEI
            """
        )
        wave_rows = cursor.fetchall() or []
        cursor.execute(
            """
            SELECT device_id, lng, lat, updated_at
            FROM device_location_latest
            WHERE device_id IS NOT NULL AND device_id <> ''
            """
        )
        location_rows = cursor.fetchall() or []
        cursor.execute("SELECT imei, cable_key FROM wave_device_bind")
        binding_rows = cursor.fetchall() or []

    wave_by_id = {str(row["device_id"]): row for row in wave_rows}
    location_by_id = {str(row["device_id"]): row for row in location_rows}
    cable_by_id = {
        str(row["imei"]): str(row.get("cable_key") or "") for row in binding_rows
    }
    device_ids = sorted(set(wave_by_id) | set(location_by_id))
    now = datetime.now()
    result: list[dict[str, Any]] = []

    for device_id in device_ids:
        wave = wave_by_id.get(device_id)
        location = location_by_id.get(device_id)
        last_wave = wave.get("last_wave_time") if wave else None
        last_location = location.get("updated_at") if location else None
        activity_values = [
            value for value in (last_wave, last_location) if isinstance(value, datetime)
        ]
        last_activity = max(activity_values) if activity_values else None
        online = bool(last_activity and (now - last_activity).total_seconds() <= 90)
        has_wave = wave is not None
        has_location = location is not None
        category = "integrated" if has_wave and has_location else "monitor" if has_wave else "locator"
        lng = float(location.get("lng")) if location and location.get("lng") is not None else 0.0
        lat = float(location.get("lat")) if location and location.get("lat") is not None else 0.0

        result.append(
            {
                "id": device_id,
                "name": f"监测终端 {device_id}" if has_wave else f"定位终端 {device_id}",
                "category": category,
                "lineId": cable_by_id.get(device_id, ""),
                "position": "未配置位置",
                "coordinate": [lng, lat],
                "linkedLocatorId": "",
                "state": "online" if online else "offline",
                "enabled": True,
                "lastSeen": _timestamp(last_activity) if last_activity else "暂无上报",
                "latencyMs": None,
                "signalDbm": None,
                "firmwareVersion": "",
                "updatedAt": _timestamp(last_activity),
                "network": {
                    "accessMode": "cellular",
                    "protocol": "MQTT",
                    "ipAddress": "",
                    "serverPort": "1883",
                    "uploadTopic": "device/ac07e57d/up" if has_wave else "device/bds/up",
                    "heartbeatSeconds": "30",
                    "retrySeconds": "5",
                    "timeoutSeconds": "10",
                    "offlineThresholdSeconds": "90",
                },
                "acquisition": {
                    "batchSize": 4000,
                    "uploadMode": "sequential",
                    "batchTimeoutSeconds": "30",
                    "missingDataPolicy": "mark-and-upload",
                    "sampleRate": "",
                    "triggerThreshold": "",
                    "offlineCacheBatches": "",
                    "backfillEnabled": False,
                }
                if has_wave
                else None,
            }
        )
    return result


def _merge_runtime_devices(
    managed_devices: list[dict[str, Any]],
    runtime_devices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {str(item["id"]): dict(item) for item in managed_devices}
    for runtime in runtime_devices:
        device_id = str(runtime["id"])
        configured = merged.get(device_id)
        if configured is None:
            merged[device_id] = runtime
            continue

        # 设备名称、类型和参数以人工配置为准；在线状态、最后上报和真实坐标实时刷新。
        configured["state"] = runtime["state"]
        configured["lastSeen"] = runtime["lastSeen"]
        configured["updatedAt"] = runtime["updatedAt"]
        if not configured.get("lineId") and runtime.get("lineId"):
            configured["lineId"] = runtime["lineId"]
        if runtime.get("coordinate") != [0.0, 0.0]:
            configured["coordinate"] = runtime["coordinate"]
    return [merged[key] for key in sorted(merged)]


def _legacy_cable_lines(
    conn,
    devices: list[dict[str, Any]],
    existing_line_ids: set[str],
) -> list[dict[str, Any]]:
    """兼容既有 cable_info / wave_device_bind，让旧数据立即出现在新前端。"""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT cable_key, cable_name, cable_length, remark, updated_at
            FROM cable_info
            ORDER BY cable_key
            """
        )
        rows = cursor.fetchall() or []

    result = []
    for row in rows:
        line_id = str(row.get("cable_key") or "")
        if not line_id or line_id in existing_line_ids:
            continue
        bound_devices = [item for item in devices if item.get("lineId") == line_id]
        result.append(
            {
                "id": line_id,
                "name": row.get("cable_name") or line_id,
                "type": "land",
                "lengthKm": str(row.get("cable_length") or ""),
                "description": row.get("remark") or "",
                "state": "enabled",
                "updatedAt": _timestamp(row.get("updated_at")),
                "devices": [
                    {**device, "sequence": index + 1}
                    for index, device in enumerate(bound_devices)
                ],
            }
        )
    return result


def get_snapshot(conn) -> dict[str, list[dict[str, Any]]]:
    devices = _merge_runtime_devices(list_devices(conn), _runtime_device_inventory(conn))
    lines = list_lines(conn, devices)
    lines.extend(_legacy_cable_lines(conn, devices, {str(line["id"]) for line in lines}))
    return {"devices": devices, "lines": lines}


def _rename_key(cursor, table: str, key_column: str, previous_id: str, new_id: str):
    if not previous_id or previous_id == new_id:
        return
    cursor.execute(
        f"SELECT 1 FROM {table} WHERE {key_column}=%s LIMIT 1",
        (new_id,),
    )
    if cursor.fetchone():
        raise ValueError(f"编号 {new_id} 已存在")
    cursor.execute(
        f"UPDATE {table} SET {key_column}=%s WHERE {key_column}=%s",
        (new_id, previous_id),
    )


def _upsert_device(cursor, device: ManagedDevice):
    cursor.execute(
        """
        INSERT INTO managed_device
            (device_id, line_id, category, state, enabled, payload_json)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            line_id=VALUES(line_id), category=VALUES(category),
            state=VALUES(state), enabled=VALUES(enabled),
            payload_json=VALUES(payload_json), updated_at=CURRENT_TIMESTAMP
        """,
        (
            device.id,
            device.lineId,
            device.category,
            device.state,
            device.enabled,
            _dump_payload(device),
        ),
    )


def save_device(conn, device: ManagedDevice, previous_id: str) -> dict[str, Any]:
    try:
        with conn.cursor() as cursor:
            _rename_key(cursor, "managed_device", "device_id", previous_id, device.id)
            _upsert_device(cursor, device)
        conn.commit()
        devices = list_devices(conn)
        return next(item for item in devices if item["id"] == device.id)
    except Exception:
        conn.rollback()
        raise


def save_line(conn, line: ManagedLine, previous_id: str) -> dict[str, Any]:
    try:
        with conn.cursor() as cursor:
            _rename_key(cursor, "managed_line", "line_id", previous_id, line.id)
            cursor.execute(
                """
                INSERT INTO managed_line
                    (line_id, name, line_type, length_km, description, state, payload_json)
                VALUES (%s, %s, %s, NULLIF(%s, ''), %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), line_type=VALUES(line_type),
                    length_km=VALUES(length_km), description=VALUES(description),
                    state=VALUES(state), payload_json=VALUES(payload_json),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    line.id,
                    line.name,
                    line.type,
                    line.lengthKm,
                    line.description,
                    line.state,
                    _dump_payload(line),
                ),
            )

            cursor.execute(
                "SELECT device_id FROM managed_line_device WHERE line_id=%s",
                (line.id,),
            )
            previous_device_ids = {row["device_id"] for row in (cursor.fetchall() or [])}
            next_device_ids = {device.id for device in line.devices}

            for device in line.devices:
                normalized = ManagedDevice.model_validate(
                    {**device.model_dump(exclude={"sequence"}), "lineId": line.id}
                )
                _upsert_device(cursor, normalized)

            cursor.execute("DELETE FROM managed_line_device WHERE line_id=%s", (line.id,))
            for device in line.devices:
                cursor.execute(
                    """
                    INSERT INTO managed_line_device (line_id, device_id, sequence_no)
                    VALUES (%s, %s, %s)
                    """,
                    (line.id, device.id, device.sequence),
                )

            removed_ids = previous_device_ids - next_device_ids
            if removed_ids:
                placeholders = ",".join(["%s"] * len(removed_ids))
                cursor.execute(
                    f"""
                    UPDATE managed_device SET line_id='', updated_at=CURRENT_TIMESTAMP
                    WHERE line_id=%s AND device_id IN ({placeholders})
                    """,
                    (line.id, *sorted(removed_ids)),
                )
        conn.commit()
        return next(item for item in list_lines(conn) if item["id"] == line.id)
    except Exception:
        conn.rollback()
        raise
