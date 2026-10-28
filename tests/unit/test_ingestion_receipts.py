import asyncio
import base64
import contextlib
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.errors import ServiceError
from starlette.requests import Request

from app.api import emqx
from app.services import ingestion
from app.integrations import mqtt_bridge
from app.services.ingestion_queue import DurableInbox
from app.services.ingestion_worker import process_one
from tests.unit.test_infrastructure_lifecycle import Connection, Cursor as BaseCursor
from tests.unit.test_tdr_protocol import build_frame


class Cursor(BaseCursor):
    @property
    def lastrowid(self):
        return self.cursor.lastrowid

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def execute(self, sql, args=None):
        self.conn.statements.append(sql)
        if "ON DUPLICATE KEY UPDATE" in sql:
            table = re.search(r"INSERT INTO (\w+)", sql).group(1)
            key = {"ingestion_receipt": "event_id", "tdr_parsed_sample": "sample_name", "device_location_latest": "device_id"}.get(table)
            if key:
                sql = sql.replace("ON DUPLICATE KEY UPDATE", f"ON CONFLICT ({key}) DO UPDATE SET")
                sql = re.sub(r"VALUES\((\w+)\)", r"excluded.\1", sql)
        super().execute(sql, args)


class IngestionConnection(Connection):
    def __init__(self):
        super().__init__()
        self.statements = []
        self.commits = 0
        self.fail_after_commit = None
        self.raw.create_function("IF", 3, lambda condition, a, b: a if condition else b)
        self.raw.executescript("""
            ALTER TABLE DEVICE_RAW_FRAME ADD COLUMN CABLE TEXT;
            ALTER TABLE DEVICE_RAW_FRAME ADD COLUMN PAYLOAD TEXT;
            ALTER TABLE DEVICE_RAW_FRAME ADD COLUMN PAYLOAD_LEN INTEGER;
            ALTER TABLE device_location_latest ADD COLUMN ts INTEGER DEFAULT 0;
            CREATE TABLE ingestion_receipt (
                event_id TEXT PRIMARY KEY,kind TEXT,payload_hash TEXT,raw_frame_id INTEGER,
                status TEXT DEFAULT 'pending',response_json TEXT);
            CREATE TABLE tdr_parsed_sample (
                sample_name TEXT PRIMARY KEY,raw_frame_id INTEGER,device_imei TEXT,cable_key TEXT,
                phase TEXT,waveform_4000 TEXT,device_fault_type TEXT,device_fault_distance_m NUMERIC,
                device_result_all TEXT,cable_length_m NUMERIC,device_reported_cable_length_m NUMERIC,
                payload_len INTEGER,frame_created_at TEXT,parse_status TEXT,sample_status TEXT,
                parse_message TEXT,updated_at TEXT);
        """)

    def cursor(self):
        return Cursor(self)

    def commit(self):
        super().commit()
        self.commits += 1
        if self.commits == self.fail_after_commit:
            raise RuntimeError("commit succeeded but acknowledgement lost")


class IngestionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.conn = IngestionConnection()
        self.addCleanup(self.conn.raw.close)
        db = patch.object(ingestion, "get_conn", return_value=self.conn)
        db.start()
        self.addCleanup(db.stop)
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.body = {"clientid": "D1", "topic": "device/ac07e57d/up", "payload_hex": build_frame().hex()}

    def count(self, table):
        return self.conn.raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_duplicate_wave_returns_same_frame_without_reinserting(self):
        first = ingestion.process_wave(self.body, "event-1")
        second = ingestion.process_wave(self.body, "event-1")
        self.assertEqual(first, second)
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 1)
        self.assertEqual(self.count("tdr_parsed_sample"), 1)

    def test_identical_wave_with_different_event_ids_is_two_real_frames(self):
        ingestion.process_wave(self.body, "event-1")
        ingestion.process_wave(self.body, "event-2")
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 2)

    def test_parse_database_failure_retries_existing_raw_frame(self):
        with patch.object(ingestion, "insert_tdr_parsed_sample", side_effect=RuntimeError("DB offline")):
            first = ingestion.process_wave(self.body, "event-1")
        self.assertTrue(first["retryable"])
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 1)
        second = ingestion.process_wave(self.body, "event-1")
        self.assertFalse(second["retryable"])
        self.assertEqual(second["raw_frame_id"], first["raw_frame_id"])
        self.assertEqual(self.count("tdr_parsed_sample"), 1)

    def test_retry_keeps_line_metadata_from_original_reception(self):
        with patch.object(ingestion, "insert_tdr_parsed_sample", side_effect=RuntimeError("DB offline")):
            ingestion.process_wave(self.body, "event-1")
        self.conn.raw.execute("UPDATE cable_info SET cable_length=9999 WHERE cable_key='OLD'")
        self.conn.raw.commit()
        result = ingestion.process_wave(self.body, "event-1")
        self.assertEqual(result["cable_length_m"], 1500)

    def test_lost_raw_commit_ack_does_not_duplicate_on_retry(self):
        self.conn.fail_after_commit = 1
        with self.assertRaises(ServiceError):
            ingestion.process_wave(self.body, "event-1")
        self.conn.fail_after_commit = None
        ingestion.process_wave(self.body, "event-1")
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 1)
        self.assertEqual(self.count("tdr_parsed_sample"), 1)

    def test_lost_final_commit_ack_returns_completed_receipt_on_retry(self):
        self.conn.fail_after_commit = 2
        with self.assertRaises(ServiceError):
            ingestion.process_wave(self.body, "event-1")
        self.conn.fail_after_commit = None
        result = ingestion.process_wave(self.body, "event-1")
        self.assertEqual(result["parse_status"], "success")
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 1)
        self.assertEqual(self.count("tdr_parsed_sample"), 1)

    def test_event_identifier_conflict_is_rejected(self):
        ingestion.process_wave(self.body, "event-1")
        with self.assertRaises(ServiceError) as caught:
            ingestion.process_wave({**self.body, "clientid": "other"}, "event-1")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 1)

    def test_invalid_protocol_frame_is_archived_once_and_not_retried_forever(self):
        invalid = bytearray(build_frame())
        invalid[-1] = 0
        body = {**self.body, "payload_hex": invalid.hex()}
        result = ingestion.process_wave(body, "invalid-1")
        self.assertEqual(result["parse_status"], "invalid")
        self.assertFalse(result["retryable"])
        self.assertEqual(ingestion.process_wave(body, "invalid-1"), result)
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 1)

    def test_missing_receipt_table_fails_before_raw_insert(self):
        self.conn.raw.execute("DROP TABLE ingestion_receipt")
        with self.assertRaises(ServiceError):
            ingestion.process_wave(self.body, "event-1")
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 0)

    def test_missing_receipt_table_is_reported_as_not_ready(self):
        self.conn.raw.execute("DROP TABLE ingestion_receipt")
        with self.assertRaises(ServiceError) as caught:
            ingestion.check_readiness()
        self.assertEqual(caught.exception.status_code, 503)

    def test_raw_insert_and_receipt_roll_back_together(self):
        with patch.object(ingestion.receipts, "attach_raw", side_effect=RuntimeError("receipt write failed")):
            with self.assertRaises(ServiceError):
                ingestion.process_wave(self.body, "event-1")
        self.assertEqual(self.count("DEVICE_RAW_FRAME"), 0)
        self.assertEqual(self.count("ingestion_receipt"), 0)

    def test_completed_receipt_cannot_regress_to_pending(self):
        result = ingestion.process_wave(self.body, "event-1")
        with self.conn.cursor() as cursor:
            ingestion.receipts.finish(cursor, "event-1", {"retryable": True}, done=False)
        self.conn.commit()
        self.assertEqual(ingestion.process_wave(self.body, "event-1"), result)

    def test_gps_duplicate_does_not_reexecute_update(self):
        body = {"device_id": "GPS-1", "lng": 113.5, "lat": 34.8, "ts": 2000}
        self.assertEqual(ingestion.process_gps(body, "gps-1"), ingestion.process_gps(body, "gps-1"))
        updates = [sql for sql in self.conn.statements if "INSERT INTO device_location_latest" in sql]
        self.assertEqual(len(updates), 1)

    def test_old_gps_retry_does_not_replace_newer_position(self):
        ingestion.process_gps({"device_id": "GPS-1", "lng": 113.5, "lat": 34.8, "ts": 2000}, "gps-2")
        ingestion.process_gps({"device_id": "GPS-1", "lng": 100, "lat": 30, "ts": 1000}, "gps-1")
        row = self.conn.raw.execute("SELECT lng,ts FROM device_location_latest WHERE device_id='GPS-1'").fetchone()
        self.assertEqual(tuple(row), (113.5, 2000))

    def test_old_http_without_event_key_remains_supported(self):
        self.assertEqual(ingestion.process_wave(self.body)["parse_status"], "success")
        self.assertEqual(self.count("ingestion_receipt"), 0)

    def test_queue_http_timeout_after_mysql_success_retries_without_duplicate(self):
        body = {
            "clientid": "D1", "topic": "device/ac07e57d/up",
            "payload_b64": base64.b64encode(build_frame()).decode(),
        }
        identifiers = []

        def post(_url, *, json, headers, timeout):
            identifiers.append(headers["Idempotency-Key"])
            result = ingestion.process_wave(json, headers["Idempotency-Key"])
            if len(identifiers) == 1:
                raise mqtt_bridge.requests.Timeout("HTTP response lost after commit")
            response = MagicMock(status_code=200)
            response.json.return_value = result
            return response

        with tempfile.TemporaryDirectory() as temporary:
            inbox = DurableInbox(Path(temporary) / "inbox.sqlite3")
            try:
                inbox.enqueue("broker", "wave", json.dumps(body).encode(), mid=1)
                with patch.object(mqtt_bridge.requests, "post", side_effect=post):
                    process_one(inbox, "wave", mqtt_bridge._deliver)
                    with patch("app.services.ingestion_queue.time.time", return_value=10**12):
                        process_one(inbox, "wave", mqtt_bridge._deliver)
                self.assertEqual(identifiers[0], identifiers[1])
                self.assertEqual(inbox.counts(), {"done": 1})
                self.assertEqual(self.count("DEVICE_RAW_FRAME"), 1)
                self.assertEqual(self.count("tdr_parsed_sample"), 1)
            finally:
                inbox.close()

    def test_wave_and_gps_database_work_do_not_block_async_event_loop(self):
        async def scenario(endpoint, name):
            release, started = threading.Event(), threading.Event()

            def slow(*_):
                started.set()
                release.wait(2)
                return {"status": "ok"}

            async def receive():
                return {"type": "http.request", "body": b'{}', "more_body": False}

            request = Request({"type": "http", "headers": []}, receive)
            with patch.object(ingestion, name, side_effect=slow):
                task = asyncio.create_task(endpoint(request))
                try:
                    for _ in range(100):
                        if started.is_set():
                            break
                        await asyncio.sleep(0.005)
                    self.assertTrue(started.is_set())
                    self.assertFalse(task.done())
                    await asyncio.sleep(0.01)
                finally:
                    release.set()
                self.assertEqual(await asyncio.wait_for(task, 2), {"status": "ok"})

        asyncio.run(scenario(emqx.emqx_webhook, "process_wave"))
        asyncio.run(scenario(emqx.emqx_gps, "process_gps"))
