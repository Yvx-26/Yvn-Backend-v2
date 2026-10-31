"""未完成数据迁移前保留的旧配置、旧线路台账适配。"""

from typing import Any
from app.utils.units import format_metres
from app.utils.serialization import timestamp_text


def normalize_device_config(data):
    acquisition = data.get("acquisition")
    if isinstance(acquisition, dict):
        # 兼容早期前端把一帧误建模为“10 条相间批次”的已保存配置。
        if acquisition.get("batchSize") == 10:
            acquisition["batchSize"] = 4000
        if acquisition.get("triggerThreshold") == "2.50 V":
            acquisition["triggerThreshold"] = ""
    return data


def legacy_cable_lines(
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
        bound_devices = [
            item for item in devices
            if item.get("lineId") == line_id and item.get("category") != "monitor"
        ]
        result.append(
            {
                "id": line_id,
                "name": row.get("cable_name") or line_id,
                "type": "land",
                "lengthM": format_metres(row.get("cable_length")),
                "description": row.get("remark") or "",
                "state": "enabled",
                "updatedAt": timestamp_text(row.get("updated_at")),
                "devices": [
                    {**device, "sequence": index + 1}
                    for index, device in enumerate(bound_devices)
                ],
            }
        )
    return result
