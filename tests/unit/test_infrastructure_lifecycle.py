"""内存数据库回归：执行服务的关系写入、外键、回滚和重读。

适配少量 MySQL 语法，不代替隔离 MySQL 的并发/锁测试。
"""

import asyncio
import json
import re
import sqlite3
import unittest
from unittest.mock import patch

from app.api import cable, device as device_api
from app.api import infrastructure as infrastructure_api
from app.main import app
from app.repositories.line_binding import get_bound_cable_info
from app.schemas.cable import CableInfoIn
from app.schemas.device import WaveDeviceBindIn
from app.schemas.infrastructure import ManagedDevice, ManagedLineWrite
from app.services.infrastructure import get_snapshot, save_device, save_line
from app.repositories.line_binding import get_device_binding


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.cursor = conn.raw.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, args=None):
        if self.conn.fail_membership and "INSERT INTO managed_line_device" in sql:
            raise RuntimeError("simulated membership failure")
        sql = sql.replace(" FOR UPDATE", "").replace("%s", "?")
        if "ON DUPLICATE KEY UPDATE" in sql:
            table = re.search(r"INSERT INTO (\w+)", sql).group(1)
            key = {"managed_device": "device_id", "managed_line": "line_id"}[table]
            sql = sql.replace("ON DUPLICATE KEY UPDATE", f"ON CONFLICT ({key}) DO UPDATE SET")
            sql = re.sub(r"VALUES\((\w+)\)", r"excluded.\1", sql)
        self.cursor.execute(sql, args or ())

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


class Connection:
    def __init__(self):
        # FastAPI 同步路由在线程池执行；测试连接顺序复用，不并行使用。
        self.raw = sqlite3.connect(":memory:", check_same_thread=False)
        self.raw.row_factory = sqlite3.Row
        self.raw.execute("PRAGMA foreign_keys=ON")
        self.raw.create_collation("utf8mb4_unicode_ci", lambda a, b: (a > b) - (a < b))
        self.fail_membership = False
        self.raw.executescript("""
            CREATE TABLE managed_line (
                line_id TEXT PRIMARY KEY, name TEXT, line_type TEXT, length_m NUMERIC,
                description TEXT, state TEXT, payload_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE managed_device (
                device_id TEXT PRIMARY KEY, line_id TEXT, category TEXT, state TEXT,
                enabled INTEGER, payload_json TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE managed_line_device (
                line_id TEXT REFERENCES managed_line(line_id) ON UPDATE CASCADE,
                device_id TEXT REFERENCES managed_device(device_id) ON UPDATE CASCADE,
                sequence_no INTEGER, PRIMARY KEY(line_id,device_id), UNIQUE(line_id,sequence_no));
            CREATE TABLE DEVICE_RAW_FRAME (ID INTEGER PRIMARY KEY, IMEI TEXT, CREATED_AT TIMESTAMP);
            CREATE TABLE device_location_latest (device_id TEXT PRIMARY KEY, lng NUMERIC, lat NUMERIC, updated_at TIMESTAMP);
            CREATE TABLE wave_device_bind (imei TEXT PRIMARY KEY, cable_key TEXT, updated_at TIMESTAMP);
            CREATE TABLE cable_info (cable_key TEXT PRIMARY KEY, cable_name TEXT, cable_length NUMERIC, remark TEXT, updated_at TIMESTAMP);
            INSERT INTO cable_info VALUES ('OLD','Legacy',1500,'','old-time');
            INSERT INTO device_location_latest VALUES ('D1',113.5,34.8,NULL),('D2',113.6,34.9,NULL);
            INSERT INTO wave_device_bind VALUES ('D1','OLD',NULL),('D2','OLD',NULL);
        """)

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        # Legacy API closes its acquired connection; the fixture owns the actual lifetime.
        pass


class InfrastructureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = Connection()
        self.addCleanup(self.conn.raw.close)

    def line(self, line_id, *members):
        return ManagedLineWrite(
            id=line_id, name=line_id, type="land", lengthM="1500", state="enabled",
            devices=[{"id": value, "sequence": index + 1} for index, value in enumerate(members)],
        )

    def configure_monitor(self):
        return save_device(self.conn, ManagedDevice(
            id="M1", name="Monitor", category="monitor", lineId="OLD",
            coordinate=(0, 0), linkedLocatorId="D1",
            network={"accessMode": "cellular", "protocol": "MQTT"},
        ), "M1")

    def snapshot_devices(self):
        return {item["id"]: item for item in get_snapshot(self.conn)["devices"]}

    def test_legacy_import_rename_move_unbind_and_reload(self):
        self.configure_monitor()
        snapshot = get_snapshot(self.conn)
        self.assertEqual([item["id"] for item in snapshot["lines"][0]["devices"]], ["D1", "D2"])
        saved = save_line(self.conn, self.line("NEW", "D2", "D1"), "OLD")
        self.assertEqual([item["id"] for item in saved["devices"]], ["D2", "D1"])
        self.assertEqual([item["id"] for item in get_snapshot(self.conn)["lines"]], ["NEW"])
        self.assertEqual(self.snapshot_devices()["M1"]["lineId"], "NEW")
        save_line(self.conn, self.line("L2", "D1"), "L2")
        self.assertEqual(self.snapshot_devices()["M1"]["linkedLocatorId"], "")
        d1 = ManagedDevice.model_validate({**self.snapshot_devices()["D1"], "lineId": ""})
        save_device(self.conn, d1, "D1")
        self.assertEqual(self.snapshot_devices()["D1"]["lineId"], "")
        with self.conn.cursor() as cur:
            self.assertIsNone(get_device_binding(cur, "D1")["cable_key"])
            self.assertEqual(get_bound_cable_info(cur, "M1"), ("NEW", 0.0))

    def test_membership_failure_rolls_back_line_devices_and_links(self):
        self.configure_monitor()
        self.conn.fail_membership = True
        with self.assertRaisesRegex(RuntimeError, "membership failure"):
            save_line(self.conn, self.line("L2", "D1"), "L2")
        self.conn.fail_membership = False
        snapshot = get_snapshot(self.conn)
        self.assertEqual([item["id"] for item in snapshot["lines"]], ["OLD"])
        self.assertEqual(self.snapshot_devices()["D1"]["lineId"], "OLD")
        self.assertEqual(self.snapshot_devices()["M1"]["linkedLocatorId"], "D1")

    def test_legacy_writers_update_canonical_managed_records(self):
        self.configure_monitor()
        save_line(self.conn, self.line("L2"), "L2")
        with patch.object(device_api, "get_conn", return_value=self.conn):
            device_api.api_save_device_bind(WaveDeviceBindIn(imei="D1", cable_key="L2"))
        self.assertEqual(self.snapshot_devices()["D1"]["lineId"], "L2")
        with patch.object(cable, "get_conn", return_value=self.conn):
            cable.save_cable_info(CableInfoIn(cable_key="L2", cable_name="Edited", cable_length=2500))
            self.assertEqual(float(cable.get_cable_info("L2")["cable_length"]), 0)
        with self.conn.cursor() as cur:
            self.assertEqual(get_bound_cable_info(cur, "D1"), ("L2", 0.0))

    def request_line(self, members):
        body = json.dumps({
            "id": "OLD", "name": "Legacy", "type": "land", "lengthM": "1500",
            "state": "enabled", "devices": members,
        }).encode()
        messages = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "PUT", "scheme": "http", "path": "/api/infrastructure/lines/OLD",
            "raw_path": b"/api/infrastructure/lines/OLD", "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1), "server": ("test", 80),
        }
        # Only HTTP scope is called, so MQTT lifespan never starts.
        with patch.object(infrastructure_api, "get_conn", return_value=self.conn):
            asyncio.run(app(scope, receive, send))
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        data = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return status, json.loads(data)

    def test_http_reference_write_returns_full_current_device_details(self):
        self.configure_monitor()
        status, response = self.request_line([{"id": "D2", "sequence": 1}, {"id": "D1", "sequence": 2}])
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in response["devices"]], ["D2", "D1"])
        self.assertIn("network", response["devices"][0])

    def test_http_legacy_full_nodes_remain_accepted(self):
        self.configure_monitor()
        nodes = get_snapshot(self.conn)["lines"][0]["devices"]
        self.assertEqual(self.request_line(nodes)[0], 200)

    def test_http_duplicate_sequence_is_rejected_without_changing_membership(self):
        self.configure_monitor()
        status, _ = self.request_line([{"id": "D1", "sequence": 1}, {"id": "D2", "sequence": 1}])
        self.assertEqual(status, 422)
        self.assertEqual(len(get_snapshot(self.conn)["lines"][0]["devices"]), 2)
