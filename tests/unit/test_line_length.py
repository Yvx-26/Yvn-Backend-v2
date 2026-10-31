import unittest

from app.utils.line_length import route_length_metres
from app.services.infrastructure import save_line, save_device, get_snapshot
from app.schemas.infrastructure import ManagedDevice
from tests.unit import test_infrastructure_lifecycle as lifecycle


def node(sequence, coordinate):
    return {"sequence": sequence, "coordinate": coordinate}


class RouteLengthTests(unittest.TestCase):
    def test_geometry_and_missing_points(self):
        self.assertAlmostEqual(float(route_length_metres([node(2, [2, 0]), node(1, [1, 0])])), 111195.080, places=3)
        self.assertAlmostEqual(float(route_length_metres([node(1, [1, 0]), node(3, [2, 0]), node(2, [3, 0])])), 333585.241, places=3)
        self.assertEqual(route_length_metres([]), '0.000')
        self.assertEqual(route_length_metres([node(1, [1, 0])]), '0.000')
        for point in ([0, 0], [181, 0], [float('nan'), 20], None):
            self.assertEqual(route_length_metres([node(1, [1, 0]), node(2, point), node(3, [2, 0])]), '')

    def test_saved_length_uses_latest_nodes_and_updates_after_device_coordinate_edit(self):
        conn = lifecycle.Connection()
        self.addCleanup(conn.raw.close)
        fixture = lifecycle.InfrastructureLifecycleTests()
        fixture.conn = conn
        fixture.configure_monitor()
        line = fixture.line('OLD', 'D1', 'D2')
        line.lengthM = '999999'
        saved = save_line(conn, line, 'OLD')
        expected = route_length_metres(saved['devices'])
        self.assertEqual(saved['lengthM'], expected)
        self.assertNotEqual(expected, '999999')
        with conn.cursor() as cursor:
            cursor.execute('SELECT length_m FROM managed_line WHERE line_id=%s', ('OLD',))
            self.assertAlmostEqual(float(cursor.fetchone()['length_m']), float(expected), places=3)
        device = next(item for item in get_snapshot(conn)['devices'] if item['id'] == 'D2')
        save_device(conn, ManagedDevice.model_validate({**device, 'coordinate': [113.7, 35.0]}), 'D2')
        with conn.cursor() as cursor:
            cursor.execute('SELECT length_m FROM managed_line WHERE line_id=%s', ('OLD',))
            self.assertGreater(float(cursor.fetchone()['length_m']), float(expected))
        save_line(conn, fixture.line('OLD', 'D1'), 'OLD')
        with conn.cursor() as cursor:
            cursor.execute('SELECT length_m FROM managed_line WHERE line_id=%s', ('OLD',))
            self.assertEqual(float(cursor.fetchone()['length_m']), 0)
