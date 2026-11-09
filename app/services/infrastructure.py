"""线路与设备资料的 MySQL 持久化服务。"""

import json
from typing import Any

from app.schemas.infrastructure import BoundLineDevice, ManagedDevice, ManagedLine, ManagedLineWrite
from app.services.device_inventory import discover_runtime_devices, merge_runtime_devices
from app.services.compat.infrastructure import legacy_cable_lines, normalize_device_config
from app.utils.serialization import json_object, timestamp_text
from app.utils.units import format_metres
from app.utils.line_length import route_length_metres


def _dump_payload(model: ManagedDevice | ManagedLine | ManagedLineWrite) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _device_from_row(row: dict[str, Any]) -> dict[str, Any]:
    data = json_object(row.get("payload_json"))
    data = normalize_device_config(data)
    data.update(
        {
            "id": row.get("device_id") or "",
            "lineId": row.get("line_id") or "",
            "category": row.get("category") or "integrated",
            "state": row.get("state") or "offline",
            "enabled": bool(row.get("enabled")),
            "updatedAt": timestamp_text(row.get("updated_at")),
        }
    )
    return data


def list_devices(conn, *, for_update: bool = False) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT device_id, line_id, category, state, enabled, payload_json, updated_at
            FROM managed_device
            ORDER BY device_id
            """ + (" FOR UPDATE" if for_update else "")
        )
        rows = cursor.fetchall() or []
    return [_device_from_row(row) for row in rows]


def list_lines(conn, devices: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    device_items = devices if devices is not None else list_devices(conn)
    device_by_id = {item["id"]: item for item in device_items}

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT line_id, name, line_type, length_m, description, state,
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
        if (not device or device.get("category") == "monitor"
                or device.get("lineId") != binding["line_id"]):
            continue
        bindings.setdefault(binding["line_id"], []).append(
            {**device, "sequence": int(binding.get("sequence_no") or 0)}
        )

    result = []
    for row in line_rows:
        data = json_object(row.get("payload_json"))
        data.pop("lengthKm", None)
        data.update(
            {
                "id": row.get("line_id") or "",
                "name": row.get("name") or "",
                "type": row.get("line_type") or "land",
                "lengthM": route_length_metres(bindings.get(row.get("line_id"), [])),
                "description": row.get("description") or "",
                "state": row.get("state") or "draft",
                "updatedAt": timestamp_text(row.get("updated_at")),
                "devices": bindings.get(row.get("line_id"), []),
            }
        )
        result.append(data)
    return result


def _persist_route_lengths(conn, devices, affected_lines):
    """在保存资料的同一事务中刷新线路长度，包含被移出节点的原线路。"""
    for line in list_lines(conn, devices):
        if line["id"] not in affected_lines:
            continue
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE managed_line SET length_m=NULLIF(%s,''), updated_at=CURRENT_TIMESTAMP WHERE line_id=%s",
                (line["lengthM"], line["id"]),
            )


def get_snapshot(conn) -> dict[str, list[dict[str, Any]]]:
    managed = list_devices(conn)
    devices = merge_runtime_devices(managed, discover_runtime_devices(conn, managed))
    lines = list_lines(conn, devices)
    lines.extend(legacy_cable_lines(conn, devices, {str(line["id"]) for line in lines}))
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


def _repair_links(devices: dict[str, dict[str, Any]], affected: set[str]):
    """以变更后的所属线路校验关联，避免把旧快照再次写回。"""
    for item in devices.values():
        linked_id = item.get("linkedLocatorId")
        if not linked_id or (item["id"] not in affected and linked_id not in affected):
            continue
        target = devices.get(linked_id)
        valid = (
            target is not None and target["id"] != item["id"]
            and target.get("category") != "monitor"
            and bool(item.get("lineId")) and item.get("lineId") == target.get("lineId")
        )
        if not valid:
            item["linkedLocatorId"] = ""


def _ensure_managed_line(cursor, line_id: str, inventory: dict[str, dict[str, Any]]):
    if not line_id:
        return
    cursor.execute("SELECT 1 FROM managed_line WHERE line_id=%s", (line_id,))
    if cursor.fetchone():
        return
    cursor.execute(
        "SELECT cable_key, cable_name, cable_length, remark FROM cable_info WHERE cable_key=%s",
        (line_id,),
    )
    legacy = cursor.fetchone()
    if not legacy:
        raise ValueError(f"线路 {line_id} 不存在")
    line = ManagedLine(
        id=line_id, name=legacy.get("cable_name") or line_id, type="land",
        lengthM=format_metres(legacy.get("cable_length")),
        description=legacy.get("remark") or "", state="enabled",
    )
    cursor.execute(
        "INSERT INTO managed_line (line_id,name,line_type,length_m,description,state,payload_json) "
        "VALUES (%s,%s,%s,NULLIF(%s,''),%s,%s,%s)",
        (line.id, line.name, line.type, line.lengthM, line.description, line.state, _dump_payload(line)),
    )
    # 首次建档旧线路时一并保留已有定位顺序，不能让其他节点消失。
    members = sorted(
        (item for item in inventory.values()
         if item.get("lineId") == line_id and item.get("category") != "monitor"),
        key=lambda item: item["id"],
    )
    for sequence, item in enumerate(members, 1):
        _upsert_device(cursor, ManagedDevice.model_validate(item))
        cursor.execute("DELETE FROM managed_line_device WHERE device_id=%s", (item["id"],))
        cursor.execute(
            "INSERT INTO managed_line_device (line_id,device_id,sequence_no) VALUES (%s,%s,%s)",
            (line_id, item["id"], sequence),
        )


def save_device(conn, device: ManagedDevice, previous_id: str) -> dict[str, Any]:
    try:
        configured_devices = list_devices(conn, for_update=True)
        by_id = {item["id"]: item for item in discover_runtime_devices(conn)}
        by_id.update({item["id"]: dict(item) for item in configured_devices})
        by_id.pop(previous_id, None)
        by_id[device.id] = device.model_dump()
        if previous_id != device.id:
            for item in by_id.values():
                if item.get("linkedLocatorId") == previous_id:
                    item["linkedLocatorId"] = device.id
        _repair_links(by_id, {previous_id, device.id})
        normalized = ManagedDevice.model_validate(by_id[device.id])
        with conn.cursor() as cursor:
            _rename_key(cursor, "managed_device", "device_id", previous_id, device.id)
            _ensure_managed_line(cursor, normalized.lineId, by_id)
            _upsert_device(cursor, normalized)
            # The FK cascades a renamed ID, but category/line changes need explicit membership updates.
            cursor.execute(
                "SELECT line_id, sequence_no FROM managed_line_device WHERE device_id=%s",
                (device.id,),
            )
            bindings = cursor.fetchall() or []
            retained_sequence = next(
                (row["sequence_no"] for row in bindings if row["line_id"] == device.lineId), None
            )
            cursor.execute("DELETE FROM managed_line_device WHERE device_id=%s", (device.id,))
            if device.lineId and device.category != "monitor":
                if retained_sequence is None:
                    cursor.execute(
                        "SELECT sequence_no FROM managed_line_device WHERE line_id=%s "
                        "ORDER BY sequence_no DESC LIMIT 1 FOR UPDATE", (device.lineId,),
                    )
                    last = cursor.fetchone()
                    retained_sequence = int(last["sequence_no"]) + 1 if last else 1
                cursor.execute(
                    "INSERT INTO managed_line_device (line_id, device_id, sequence_no) VALUES (%s,%s,%s)",
                    (device.lineId, device.id, retained_sequence),
                )
            for item in configured_devices:
                if item["id"] in {previous_id, device.id}:
                    continue
                updated = by_id[item["id"]]
                if updated != item:
                    _upsert_device(cursor, ManagedDevice.model_validate(updated))
        devices = list_devices(conn)
        affected_lines = {normalized.lineId, *(row["line_id"] for row in bindings)}
        affected_lines.update(item["lineId"] for item in configured_devices if item["id"] == previous_id)
        _persist_route_lengths(conn, devices, affected_lines)
        result = next(item for item in devices if item["id"] == device.id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def save_line(conn, line: ManagedLine | ManagedLineWrite, previous_id: str) -> dict[str, Any]:
    try:
        configured_devices = list_devices(conn, for_update=True)
        original = {item["id"]: item for item in configured_devices}
        member_ids = {item.id for item in line.devices}
        by_id = {
            item["id"]: item for item in discover_runtime_devices(conn)
            if item.get("lineId") in {previous_id, line.id} or item["id"] in member_ids
        }
        by_id.update({key: dict(item) for key, item in original.items()})
        for member in line.devices:
            if member.id not in by_id:
                if not isinstance(member, BoundLineDevice):
                    raise ValueError(f"设备 {member.id} 不存在，请先配置设备")
                by_id[member.id] = ManagedDevice.model_validate(
                    member.model_dump(exclude={"sequence"})
                ).model_dump()
            if by_id[member.id].get("category") == "monitor":
                raise ValueError("线路顺序只能绑定定位设备或一体化终端")
        affected = {item.id for item in line.devices}
        line.lengthM = route_length_metres([
            {**by_id[member.id], "sequence": member.sequence} for member in line.devices
        ])
        with conn.cursor() as cursor:
            _rename_key(cursor, "managed_line", "line_id", previous_id, line.id)
            if previous_id != line.id:
                # 同步旧编号入口，防止旧线路以兼容数据再次出现；不改变历史样本记录。
                cursor.execute("SELECT 1 FROM cable_info WHERE cable_key=%s", (line.id,))
                if cursor.fetchone():
                    raise ValueError(f"旧资料中线路编号 {line.id} 已存在")
                cursor.execute("UPDATE cable_info SET cable_key=%s WHERE cable_key=%s", (line.id, previous_id))
                cursor.execute("UPDATE wave_device_bind SET cable_key=%s WHERE cable_key=%s", (line.id, previous_id))
                for item in by_id.values():
                    if item.get("lineId") == previous_id:
                        item["lineId"] = line.id
                        affected.add(item["id"])
            cursor.execute(
                """
                INSERT INTO managed_line
                    (line_id, name, line_type, length_m, description, state, payload_json)
                VALUES (%s, %s, %s, NULLIF(%s, ''), %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), line_type=VALUES(line_type),
                    length_m=VALUES(length_m), description=VALUES(description),
                    state=VALUES(state), payload_json=VALUES(payload_json),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    line.id,
                    line.name,
                    line.type,
                    line.lengthM,
                    line.description,
                    line.state,
                    # 排序只存关系，不再额外存一份会过期的完整设备配置。
                    json.dumps(line.model_dump(exclude={"devices"}), ensure_ascii=False),
                ),
            )

            cursor.execute(
                "SELECT device_id FROM managed_line_device WHERE line_id=%s",
                (line.id,),
            )
            monitor_ids = {item["id"] for item in by_id.values() if item.get("category") == "monitor"}
            previous_device_ids = {
                row["device_id"] for row in (cursor.fetchall() or [])
                if row["device_id"] not in monitor_ids
            }
            previous_device_ids.update(
                item["id"] for item in by_id.values()
                if item.get("lineId") == line.id and item.get("category") != "monitor"
            )
            next_device_ids = {device.id for device in line.devices}
            removed_ids = previous_device_ids - next_device_ids
            affected.update(removed_ids)
            for device_id in removed_ids:
                if device_id in by_id and by_id[device_id].get("lineId") == line.id:
                    by_id[device_id]["lineId"] = ""
            for device_id in next_device_ids:
                by_id[device_id]["lineId"] = line.id
            _repair_links(by_id, affected)
            for device_id, item in by_id.items():
                if item != original.get(device_id):
                    _upsert_device(cursor, ManagedDevice.model_validate(item))
            for device in line.devices:
                cursor.execute(
                    "DELETE FROM managed_line_device WHERE device_id=%s AND line_id<>%s",
                    (device.id, line.id),
                )

            cursor.execute("DELETE FROM managed_line_device WHERE line_id=%s", (line.id,))
            for device in line.devices:
                cursor.execute(
                    """
                    INSERT INTO managed_line_device (line_id, device_id, sequence_no)
                    VALUES (%s, %s, %s)
                    """,
                    (line.id, device.id, device.sequence),
                )

        saved_devices = list(by_id.values())
        affected_lines = {line.id, previous_id}
        affected_lines.update(original[item]["lineId"] for item in member_ids if item in original)
        _persist_route_lengths(conn, saved_devices, affected_lines)
        result = next(item for item in list_lines(conn, saved_devices) if item["id"] == line.id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
