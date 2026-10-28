import unittest
from unittest.mock import MagicMock, patch

from app.schemas.infrastructure import ManagedDevice, ManagedLine, ManagedLineWrite
from pydantic import ValidationError
from app.services import infrastructure as service


def device(device_id="D1", line_id="L1", category="locator", linked=""):
    return ManagedDevice(
        id=device_id, name=device_id, category=category, lineId=line_id,
        coordinate=(113.5, 34.8), linkedLocatorId=linked,
        network={"accessMode": "ethernet", "protocol": "MQTT"},
    ).model_dump()


class InfrastructureRelationTests(unittest.TestCase):
    def setUp(self):
        runtime = patch.object(service, "discover_runtime_devices", return_value=[])
        self.runtime = runtime.start()
        self.addCleanup(runtime.stop)

    def test_explicit_unbind_is_not_filled_from_legacy_runtime(self):
        configured = device(line_id="")
        runtime = {**device(), "lastSeen": "now", "updatedAt": "now"}
        result = service.merge_runtime_devices([configured], [runtime])
        self.assertEqual(result[0]["lineId"], "")

    def test_old_line_length_is_converted_from_metres_to_kilometres(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            {"cable_key": "L1", "cable_length": "794480"},
        ]
        self.assertEqual(service.legacy_cable_lines(conn, [], set())[0]["lengthKm"], "794.48")

    def test_mismatched_binding_is_not_shown_in_a_managed_line(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.side_effect = [
            [{"line_id": "L1"}],
            [{"line_id": "L1", "device_id": "D1", "sequence_no": 1}],
        ]
        self.assertEqual(service.list_lines(conn, [device(line_id="L2")])[0]["devices"], [])

    def save_line(self, configured, members, previous_members):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"device_id": value} for value in previous_members]
        line = ManagedLine(
            id="L2", name="L2", type="land", state="enabled",
            devices=[{**item, "sequence": index + 1} for index, item in enumerate(members)],
        )
        with patch.object(service, "list_devices", return_value=configured), \
             patch.object(service, "list_lines", return_value=[line.model_dump()]), \
             patch.object(service, "_rename_key"), \
             patch.object(service, "_upsert_device") as upsert:
            service.save_line(conn, line, "L2")
        return {call.args[1].id: call.args[1] for call in upsert.call_args_list}

    def test_line_move_clears_monitor_link_in_original_line(self):
        saved = self.save_line(
            [device(), device("M1", category="monitor", linked="D1")],
            [device()], [],
        )
        self.assertEqual(saved["D1"].lineId, "L2")
        self.assertEqual(saved["M1"].linkedLocatorId, "")

    def test_stale_line_payload_does_not_overwrite_new_device_settings(self):
        current = {**device(), "name": "new device name", "firmwareVersion": "new"}
        saved = self.save_line([current], [device()], [])
        self.assertEqual(saved["D1"].name, "new device name")
        self.assertEqual(saved["D1"].firmwareVersion, "new")

    def test_removed_nodes_are_not_restored_by_link_cleanup(self):
        saved = self.save_line(
            [device(line_id="L2"), device("D2", "L2", "integrated", "D1")],
            [], ["D1", "D2"],
        )
        self.assertEqual(saved["D2"].lineId, "")
        self.assertEqual(saved["D2"].linkedLocatorId, "")

    def test_stale_membership_cannot_unbind_a_device_owned_by_another_line(self):
        saved = self.save_line([device(line_id="L1")], [], ["D1"])
        self.assertNotIn("D1", saved)

    def test_reference_contract_accepts_ids_and_rejects_duplicate_members_or_order(self):
        data = dict(id="L1", name="L1", type="land", state="enabled")
        parsed = ManagedLineWrite(**data, devices=[{"id": "D1", "sequence": 1}])
        self.assertEqual(parsed.devices[0].id, "D1")
        for members in (
            [{"id": "D1", "sequence": 1}, {"id": "D1", "sequence": 2}],
            [{"id": "D1", "sequence": 1}, {"id": "D2", "sequence": 1}],
        ):
            with self.assertRaises(ValidationError):
                ManagedLineWrite(**data, devices=members)

    def test_reference_ordering_reads_latest_device_configuration(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        value = ManagedLineWrite(
            id="L2", name="L2", type="land", state="enabled",
            devices=[{"id": "D1", "sequence": 1}],
        )
        current = {**device(), "firmwareVersion": "latest"}
        with patch.object(service, "list_devices", return_value=[current]), \
             patch.object(service, "list_lines", return_value=[{"id": "L2"}]), \
             patch.object(service, "_upsert_device") as upsert:
            service.save_line(conn, value, "L2")
        self.assertEqual(upsert.call_args.args[1].firmwareVersion, "latest")

    def test_response_read_failure_rolls_back_before_commit(self):
        conn = MagicMock()
        value = ManagedDevice.model_validate(device(line_id=""))
        with patch.object(service, "list_devices", side_effect=[[], RuntimeError("read failed")]):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                service.save_device(conn, value, value.id)
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_save_read_locks_are_outside_the_sql_literal(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        service.list_devices(conn, for_update=True)
        sql = cursor.execute.call_args.args[0]
        self.assertTrue(sql.strip().endswith("FOR UPDATE"))
        self.assertNotIn("if for_update", sql)

    def test_clearing_a_legacy_line_records_unbound_discovered_nodes(self):
        self.runtime.return_value = [device(line_id="L2")]
        saved = self.save_line([], [], [])
        self.assertEqual(saved["D1"].lineId, "")

    def test_importing_a_legacy_line_preserves_other_positioning_nodes(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [None, {"cable_name": "Legacy", "cable_length": 1500}]
        with patch.object(service, "_upsert_device") as upsert:
            service._ensure_managed_line(cursor, "L1", {"D1": device(), "D2": device("D2")})
        self.assertEqual({call.args[1].id for call in upsert.call_args_list}, {"D1", "D2"})
        inserts = [call.args[1] for call in cursor.execute.call_args_list
                   if "INSERT INTO managed_line_device" in call.args[0]]
        self.assertEqual(inserts, [("L1", "D1", 1), ("L1", "D2", 2)])


if __name__ == "__main__":
    unittest.main()
