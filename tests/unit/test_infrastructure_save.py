import unittest
from unittest.mock import MagicMock, patch

from app.schemas.infrastructure import ManagedDevice, ManagedLine
from app.services.infrastructure import legacy_cable_lines, list_lines, save_device, save_line


def device_payload(device_id="D1", line_id="L1", category="locator"):
    return {
        "id": device_id, "name": device_id, "category": category, "lineId": line_id,
        "coordinate": [113.5, 34.8], "state": "offline", "linkedLocatorId": "",
        "network": {"accessMode": "ethernet", "protocol": "MQTT"}, "acquisition": None,
    }


class InfrastructureSaveTests(unittest.TestCase):
    def setUp(self):
        runtime = patch("app.services.infrastructure.discover_runtime_devices", return_value=[])
        runtime.start()
        self.addCleanup(runtime.stop)

    def connection(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        return conn, cursor

    def test_legacy_line_keeps_monitors_out_of_positioning_order(self):
        conn, cursor = self.connection()
        cursor.fetchall.return_value = [{"cable_key": "L1", "cable_name": "Cable"}]
        devices = [device_payload("M1", category="monitor"), device_payload("D1")]
        result = legacy_cable_lines(conn, devices, set())
        self.assertEqual([item["id"] for item in result[0]["devices"]], ["D1"])
        self.assertEqual(devices[0]["lineId"], "L1")

    def test_managed_line_hides_legacy_monitor_bindings(self):
        conn, cursor = self.connection()
        cursor.fetchall.side_effect = [
            [{"line_id": "L1", "name": "Cable"}],
            [{"line_id": "L1", "device_id": "M1", "sequence_no": 1},
             {"line_id": "L1", "device_id": "D1", "sequence_no": 2}],
        ]
        result = list_lines(conn, [device_payload("M1", category="monitor"), device_payload("D1")])
        self.assertEqual([item["id"] for item in result[0]["devices"]], ["D1"])

    @patch("app.services.infrastructure._rename_key")
    @patch("app.services.infrastructure._upsert_device")
    @patch("app.services.infrastructure.list_devices")
    def test_device_move_updates_membership_and_clears_cross_line_links(self, listing, upsert, rename):
        original = ManagedDevice.model_validate(device_payload())
        monitor = ManagedDevice.model_validate({**device_payload("M1", category="monitor"), "linkedLocatorId": "D1"})
        moved = original.model_copy(update={"lineId": "L2"})
        listing.side_effect = [[original.model_dump(), monitor.model_dump()], [moved.model_dump()]]
        conn, cursor = self.connection()
        cursor.fetchall.return_value = [{"line_id": "L1", "sequence_no": 2}]
        cursor.fetchone.side_effect = [{"exists": 1}, {"sequence_no": 4}]
        result = save_device(conn, moved, "D1")
        statements = [(call.args[0], call.args[1]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("DELETE FROM managed_line_device" in sql and values == ("D1",) for sql, values in statements))
        self.assertTrue(any("INSERT INTO managed_line_device" in sql and values == ("L2", "D1", 5) for sql, values in statements))
        self.assertEqual(upsert.call_args_list[-1].args[1].linkedLocatorId, "")
        self.assertEqual(result["lineId"], "L2")
        conn.commit.assert_called_once()

    @patch("app.services.infrastructure._rename_key")
    @patch("app.services.infrastructure._upsert_device")
    @patch("app.services.infrastructure.list_devices")
    def test_device_rename_updates_dependent_locator_ids(self, listing, upsert, rename):
        monitor = ManagedDevice.model_validate({**device_payload("M1", category="monitor"), "linkedLocatorId": "D1"})
        renamed = ManagedDevice.model_validate(device_payload("D2"))
        listing.side_effect = [[monitor.model_dump()], [renamed.model_dump()]]
        conn, cursor = self.connection()
        cursor.fetchall.return_value = [{"line_id": "L1", "sequence_no": 2}]
        cursor.fetchone.return_value = {"exists": 1}
        save_device(conn, renamed, "D1")
        self.assertEqual(upsert.call_args_list[-1].args[1].linkedLocatorId, "D2")
        self.assertTrue(any(call.args[1] == ("L1", "D2", 2) for call in cursor.execute.call_args_list))

    @patch("app.services.infrastructure.list_lines")
    @patch("app.services.infrastructure._rename_key")
    @patch("app.services.infrastructure._upsert_device")
    @patch("app.services.infrastructure.list_devices")
    def test_line_rename_updates_monitor_ownership(self, listing, upsert, rename, line_listing):
        monitor = ManagedDevice.model_validate(device_payload("M1", category="monitor"))
        listing.return_value = [monitor.model_dump()]
        line = ManagedLine(id="L2", name="L2", type="land", state="draft", devices=[])
        line_listing.return_value = [line.model_dump()]
        conn, cursor = self.connection()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        save_line(conn, line, "L1")
        self.assertEqual(upsert.call_args_list[0].args[1].lineId, "L2")
        conn.commit.assert_called_once()

    @patch("app.services.infrastructure._rename_key")
    @patch("app.services.infrastructure._upsert_device", side_effect=RuntimeError("write failed"))
    @patch("app.services.infrastructure.list_devices", return_value=[])
    def test_failed_device_write_rolls_back(self, listing, upsert, rename):
        conn, _ = self.connection()
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            save_device(conn, ManagedDevice.model_validate(device_payload()), "D1")
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
