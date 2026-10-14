import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import mqtt_bridge


class MqttBridgeTests(unittest.TestCase):
    @patch.object(mqtt_bridge, "_post_json")
    def test_wave_message_keeps_emqx_publisher_clientid(self, post_json):
        body = {
            "clientid": "DTU-001",
            "topic": "device/ac07e57d/up",
            "payload_b64": "qgH+",
            "timestamp": 1_725_000_000_000,
        }
        message = SimpleNamespace(
            topic="backend/wave",
            payload=json.dumps(body).encode("utf-8"),
        )

        mqtt_bridge._forward_wave(message)

        path, forwarded = post_json.call_args.args
        self.assertEqual(path, "/emqx/webhook")
        self.assertEqual(forwarded, body)

    @patch.object(mqtt_bridge, "_post_json")
    def test_wave_messages_from_two_devices_do_not_share_an_id(self, post_json):
        for clientid in ("DTU-001", "DTU-002"):
            body = {
                "clientid": clientid,
                "topic": "device/ac07e57d/up",
                "payload_b64": "qgH+",
            }
            mqtt_bridge._forward_wave(
                SimpleNamespace(
                    topic="backend/wave",
                    payload=json.dumps(body).encode("utf-8"),
                )
            )

        forwarded_ids = [call.args[1]["clientid"] for call in post_json.call_args_list]
        self.assertEqual(forwarded_ids, ["DTU-001", "DTU-002"])

    @patch.object(mqtt_bridge, "_post_json")
    def test_wave_message_without_clientid_is_rejected(self, post_json):
        message = SimpleNamespace(
            topic="backend/wave",
            payload=b'{"topic":"device/ac07e57d/up","payload_b64":"qgH+"}',
        )

        with self.assertRaisesRegex(ValueError, "missing clientid"):
            mqtt_bridge._forward_wave(message)

        post_json.assert_not_called()

    @patch.object(mqtt_bridge, "_post_json")
    def test_gps_message_is_forwarded_with_device_id(self, post_json):
        body = {
            "device_id": "GPS-01",
            "lng": 113.5,
            "lat": 34.8,
            "ts": 1_725_000_000_000,
        }
        message = SimpleNamespace(
            topic="backend/gps",
            payload=json.dumps(body).encode("utf-8"),
        )

        mqtt_bridge._forward_gps(message)

        path, forwarded = post_json.call_args.args
        self.assertEqual(path, "/emqx/gps")
        self.assertEqual(forwarded, body)


if __name__ == "__main__":
    unittest.main()
