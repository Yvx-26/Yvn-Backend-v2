import hashlib
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.api import dashboard


def capture(name, one, many):
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = one
    cursor.fetchall.side_effect = many
    with patch.object(dashboard, "get_conn", return_value=conn):
        response = getattr(dashboard, f"api_dashboard_{name}")()
    conn.close.assert_called_once()
    return {
        "response_sha256": hashlib.sha256(json.dumps(response, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "queries": [" ".join(call.args[0].split()) for call in cursor.execute.call_args_list],
    }


def generate_contract():
    now = datetime(2026, 10, 26, 12, 0, 0)
    devices = [
        {"imei": str(index), "age_seconds": age, "cable_key": "L1", "cable_name": "Line", "last_wave_time": now}
        for index, age in enumerate((0, 90, 91, None))
    ]
    point = {"imei": "D1", "lng": Decimal("113.5"), "lat": Decimal("34.8"), "ts": 123,
             "updated_at": now, "cable_key": "L1", "cable_name": "Line", "age_seconds": 90}
    return {
        "summary/filled": capture("summary", [{"device_total": 3}, {"wave_today": 12}, {"location_today": 4}], []),
        "summary/empty": capture("summary", [None, None, None], []),
        "devices/boundaries": capture("devices", [], [devices]),
        "devices/empty": capture("devices", [], [None]),
        "map/dict": capture("map", [], [[point]]),
        "map/tuple": capture("map", [], [[tuple(point.values())]]),
        "map/empty": capture("map", [], [None]),
        "operation/empty": capture("operation", [None], [[], [], [], []]),
        "operation/filled": capture("operation", [
            {"total": 3, "first_time": now, "last_time": now + timedelta(seconds=6)},
        ], [
            [{"bucket_hour": 0, "frame_count": 1}, {"bucket_hour": 12, "frame_count": 2}],
            [{"line_id": "L1", "line_name": "Line", "frame_count": 3}],
            [{"ID": 17, "IMEI": "D1", "CABLE": "B-E", "CREATED_AT": now}],
            [{"age_seconds": 90}, {"age_seconds": 91}, {"age_seconds": None}],
        ]),
    }


class DashboardContractTests(unittest.TestCase):
    def test_statistics_status_mapping_and_queries_match_snapshot(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "dashboard_contract.json"
        expected = json.loads(path.read_text(encoding="utf-8"))
        actual = generate_contract()
        for key, value in expected.items():
            with self.subTest(case=key):
                self.assertEqual(actual[key], value)

    def test_per_device_threshold_controls_devices_map_and_online_rate(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        activity = [
            {"imei": "long", "age_seconds": 300, "offline_threshold_seconds": "300"},
            {"imei": "short", "age_seconds": 31, "offline_threshold_seconds": "30"},
            {"imei": "default", "age_seconds": 90, "offline_threshold_seconds": "invalid"},
            {"imei": "missing", "age_seconds": None, "offline_threshold_seconds": "300"},
        ]
        cursor.fetchall.side_effect = [activity, activity, [], [], [], activity]
        cursor.fetchone.return_value = None
        with patch.object(dashboard, "get_conn", return_value=conn):
            devices = dashboard.api_dashboard_devices()
            points = dashboard.api_dashboard_map()
            stats = dashboard.api_dashboard_operation()
        expected = [True, False, True, False]
        self.assertEqual([device["online"] for device in devices], expected)
        self.assertEqual([point["online"] for point in points], expected)
        self.assertEqual((stats["online_devices"], stats["device_total"], stats["online_rate"]), (2, 4, 50.0))
