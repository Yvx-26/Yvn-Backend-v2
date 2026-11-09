"""真实上报的设备发现与运行状态合并，不改写人工配置。"""

from datetime import datetime
from typing import Any

from app.repositories.device_inventory import inventory_rows
from app.services.device_presence import DEFAULT_OFFLINE_THRESHOLD_SECONDS, is_online
from app.utils.serialization import timestamp_text


def discover_runtime_devices(conn, managed_devices=()) -> list[dict[str, Any]]:
    """从真实采集表发现设备，不要求先在 managed_device 中人工建档。"""

    wave_rows, location_rows, binding_rows = inventory_rows(conn)

    wave_by_id = {str(row["device_id"]): row for row in wave_rows}
    location_by_id = {str(row["device_id"]): row for row in location_rows}
    cable_by_id = {
        str(row["imei"]): str(row.get("cable_key") or "") for row in binding_rows
    }
    device_ids = sorted(set(wave_by_id) | set(location_by_id))
    managed_by_id = {str(item["id"]): item for item in managed_devices}
    now = datetime.now()
    result: list[dict[str, Any]] = []

    for device_id in device_ids:
        wave = wave_by_id.get(device_id)
        location = location_by_id.get(device_id)
        last_wave = wave.get("last_wave_time") if wave else None
        last_location = location.get("updated_at") if location else None
        has_wave = wave is not None
        has_location = location is not None
        category = "integrated" if has_wave and has_location else "monitor" if has_wave else "locator"
        configured = managed_by_id.get(device_id, {})
        configured_category = configured.get("category", category)
        activity_values = []
        if configured_category != "locator" and isinstance(last_wave, datetime):
            activity_values.append(last_wave)
        if configured_category != "monitor" and isinstance(last_location, datetime):
            activity_values.append(last_location)
        last_activity = max(activity_values, default=None)
        age_seconds = (now - last_activity).total_seconds() if last_activity is not None else None
        network = configured.get("network")
        threshold = network.get("offlineThresholdSeconds") if isinstance(network, dict) else None
        online = is_online(age_seconds, threshold)
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
                "lastSeen": timestamp_text(last_activity) if last_activity else "暂无上报",
                "latencyMs": None,
                "signalDbm": None,
                "firmwareVersion": "",
                "updatedAt": timestamp_text(last_activity),
                "network": {
                    "accessMode": "cellular",
                    "protocol": "MQTT",
                    "ipAddress": "",
                    "serverPort": "1883",
                    "uploadTopic": "device/ac07e57d/up" if has_wave else "device/bds/up",
                    "heartbeatSeconds": "30",
                    "retrySeconds": "5",
                    "timeoutSeconds": "10",
                    "offlineThresholdSeconds": str(DEFAULT_OFFLINE_THRESHOLD_SECONDS),
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


def merge_runtime_devices(
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
        # 建档后的空线路也是明确配置，不能被旧绑定回填。
        if runtime.get("coordinate") != [0.0, 0.0]:
            configured["coordinate"] = runtime["coordinate"]
    return [merged[key] for key in sorted(merged)]
