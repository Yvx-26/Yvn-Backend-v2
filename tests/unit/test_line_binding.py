"""使用内存数据库执行真实绑定 SQL，不连接业务 MySQL。"""

import sqlite3
import unittest

from app.repositories.line_binding import get_device_binding


class Cursor:
    def __init__(self, conn):
        self.cursor = conn.cursor()

    def execute(self, sql, args):
        self.cursor.execute(sql.replace("%s", "?"), args)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None


class LineBindingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_collation("utf8mb4_unicode_ci", lambda a, b: (a > b) - (a < b))
        self.conn.executescript("""
            CREATE TABLE managed_device(device_id TEXT PRIMARY KEY, line_id TEXT, updated_at TEXT);
            CREATE TABLE managed_line(line_id TEXT PRIMARY KEY, name TEXT, length_m NUMERIC);
            CREATE TABLE wave_device_bind(imei TEXT PRIMARY KEY, cable_key TEXT, updated_at TEXT);
            CREATE TABLE cable_info(cable_key TEXT PRIMARY KEY, cable_name TEXT, cable_length NUMERIC);
            INSERT INTO cable_info VALUES ('OLD', 'Old line', 1500);
            INSERT INTO wave_device_bind VALUES ('D1', 'OLD', 'old-time');
            INSERT INTO managed_line VALUES ('NEW', 'New line', 2500);
        """)

    def read(self):
        return get_device_binding(Cursor(self.conn), "D1")

    def test_unconfigured_device_retains_legacy_binding_in_metres(self):
        row = self.read()
        self.assertEqual(row["cable_key"], "OLD")
        self.assertEqual(row["cable_length_m"], 1500)

    def test_managed_binding_wins_and_length_remains_in_metres(self):
        self.conn.execute("INSERT INTO managed_device VALUES ('D1','NEW','new-time')")
        row = self.read()
        self.assertEqual(row["cable_key"], "NEW")
        self.assertEqual(row["cable_name"], "New line")
        self.assertEqual(row["cable_length_m"], 2500)
        self.assertEqual(row["updated_at"], "new-time")

    def test_explicit_unbind_has_neither_old_line_name_nor_length(self):
        self.conn.execute("INSERT INTO managed_device VALUES ('D1','','new-time')")
        row = self.read()
        self.assertIsNone(row["cable_key"])
        self.assertIsNone(row["cable_name"])
        self.assertIsNone(row["cable_length_m"])

    def test_cleared_managed_length_does_not_fall_back_to_old_length(self):
        self.conn.execute("INSERT INTO managed_device VALUES ('D1','OLD','new-time')")
        self.conn.execute("INSERT INTO managed_line VALUES ('OLD','Edited line',NULL)")
        self.assertIsNone(self.read()["cable_length_m"])

    def test_unknown_device_returns_unbound_without_a_database_row(self):
        self.assertIsNone(get_device_binding(Cursor(self.conn), "unknown")["cable_key"])
