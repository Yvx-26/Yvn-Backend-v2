"""重构前固定的合成数据契约：逐字段响应哈希与查询条件都必须保持。"""

import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api import dashboard, waveform
from tests.unit.test_tdr_protocol import build_frame


def contract_cases():
    invalid = bytearray(build_frame())
    invalid[-1] = 0
    invalid_phase = bytearray(build_frame())
    invalid_phase[1] = 255
    payloads = {
        "valid": build_frame().hex(), "invalid_tail": invalid.hex(),
        "invalid_phase": invalid_phase.hex(), "legacy_csv": "1,2,-3,4",
        "odd_hex": "AA001", "hex_bytes": b"AA00ff", "empty_payload": "",
    }
    cases = {"missing": None}
    for name, payload in payloads.items():
        cases[name] = {
            "ID": 17, "IMEI": "D1", "CABLE": "B-E", "PAYLOAD": payload,
            "PAYLOAD_LEN": len(payload), "CREATED_AT": datetime(2026, 10, 26, 12, 34, 56, 123456),
        }
    return cases


def capture(row, mode, max_points=200000):
    conn = MagicMock()
    cur = conn.cursor.return_value
    cur.__enter__.return_value = cur
    cur.fetchone.return_value = row
    if mode == "latest":
        with patch.object(dashboard, "get_conn", return_value=conn):
            result = dashboard.api_dashboard_wave_latest("D1", None)
    elif mode == "phase":
        with patch.object(dashboard, "get_conn", return_value=conn):
            result = dashboard.api_dashboard_wave_latest("D1", "b-e")
    else:
        with patch.object(waveform, "get_conn", return_value=conn):
            result = waveform.waveform("D1", 17 if mode == "history_id" else None, 6000, max_points)
    conn.close.assert_called_once()
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    sql = [{"sql": " ".join(call.args[0].split()), "params": list(call.args[1])}
           for call in cur.execute.call_args_list]
    return {"response_sha256": digest, "queries": sql}


def generate_contract():
    result = {}
    for name, row in contract_cases().items():
        for mode in ("latest", "phase", "history_id", "history_latest"):
            result[f"{name}/{mode}"] = capture(row, mode)
    result["valid/history_truncated"] = capture(contract_cases()["valid"], "history_id", 100)
    result["invalid_tail/history_truncated"] = capture(contract_cases()["invalid_tail"], "history_id", 100)
    return result


class WaveformContractTests(unittest.TestCase):
    def test_frame_list_preserves_cursor_pagination_and_empty_page(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        rows = [{"ID": 9}, {"ID": 8}]
        cursor.fetchall.return_value = rows
        with patch.object(waveform, "get_conn", return_value=conn):
            result = waveform.list_frames("D1", 2, 10)
        self.assertEqual(result, {"data": rows, "next_before_id": 8})
        self.assertEqual(cursor.execute.call_args.args[1], ("D1", 10, 2))
        cursor.fetchall.return_value = []
        with patch.object(waveform, "get_conn", return_value=conn):
            self.assertEqual(waveform.list_frames("D1", 2, None), {"data": [], "next_before_id": None})
        self.assertEqual(cursor.execute.call_args.args[1], ("D1", 2))

    def test_device_list_keeps_existing_test_device_exclusion_query(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [{"IMEI": "D1"}]
        with patch.object(waveform, "get_conn", return_value=conn):
            self.assertEqual(waveform.list_devices(10), {"devices": ["D1"]})
        self.assertIn("NOT IN ('None', 'sub_client', 'test_client')", cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], (10,))

    def test_responses_and_query_selection_match_pre_refactor_snapshot(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "waveform_contract.json"
        expected = json.loads(path.read_text(encoding="utf-8"))
        actual = generate_contract()
        for key, value in expected.items():
            with self.subTest(case=key):
                self.assertEqual(actual[key], value)

    def test_invalid_phase_still_returns_400_before_opening_database(self):
        with patch.object(dashboard, "get_conn") as connect:
            with self.assertRaises(HTTPException) as caught:
                dashboard.api_dashboard_wave_latest("D1", "invalid")
        self.assertEqual(caught.exception.status_code, 400)
        connect.assert_not_called()
