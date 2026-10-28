import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tempfile
from pathlib import Path
from contextlib import ExitStack

from app.integrations import mqtt_bridge


class MqttBridgeTests(unittest.TestCase):
    def test_http_success_with_pending_parse_is_retried_with_same_identifier(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "ok", "ingestion_event_id": "event-1", "retryable": True}
        with patch.object(mqtt_bridge.requests, "post", return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, "pending"):
                mqtt_bridge._post_json("/emqx/webhook", {}, event_id="event-1")
        self.assertEqual(post.call_args.kwargs["headers"], {"Idempotency-Key": "event-1"})

    def test_http_rejection_is_permanent_but_server_failure_is_retryable(self):
        from app.services.ingestion_worker import PermanentDeliveryError
        response = MagicMock(status_code=422)
        with patch.object(mqtt_bridge.requests, "post", return_value=response):
            with self.assertRaises(PermanentDeliveryError):
                mqtt_bridge._post_json("/emqx/gps", {})
        response.status_code = 503
        response.raise_for_status.side_effect = mqtt_bridge.requests.HTTPError("unavailable")
        with patch.object(mqtt_bridge.requests, "post", return_value=response):
            with self.assertRaises(mqtt_bridge.requests.HTTPError):
                mqtt_bridge._post_json("/emqx/gps", {})

    def test_readiness_cache_requires_idempotent_receiver(self):
        response = MagicMock()
        response.json.return_value = {"status": "ok", "idempotency": True}
        with patch.object(mqtt_bridge, "_ready_until", 0), \
             patch.object(mqtt_bridge, "_ready_url", ""), \
             patch.object(mqtt_bridge.requests, "get", return_value=response) as get:
            mqtt_bridge._receiver_ready()
            mqtt_bridge._receiver_ready()
            self.assertEqual(get.call_count, 1)

    def test_bridge_startup_uses_manual_ack_and_persistent_session_and_closes_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for name, value in {
                "BACKEND_ROOT": root, "MQTT_INBOX_PATH": root / "inbox.sqlite3",
                "MQTT_BRIDGE_ENABLED": True, "MQTT_BROKER_HOST": "test-broker",
                "_client": None, "_runtime": None,
            }.items():
                stack.enter_context(patch.object(mqtt_bridge, name, value))
            stack.enter_context(patch.object(mqtt_bridge, "_receiver_ready", lambda: None))
            factory = stack.enter_context(patch.object(mqtt_bridge.mqtt, "Client"))
            mqtt_bridge.start_mqtt_bridge()
            runtime = mqtt_bridge._runtime
            try:
                self.assertIs(factory.call_args.kwargs["manual_ack"], True)
                self.assertIs(factory.call_args.kwargs["clean_session"], False)
                self.assertTrue(all(worker.thread.is_alive() for worker in runtime.workers))
            finally:
                mqtt_bridge.stop_mqtt_bridge()
            self.assertTrue(all(not worker.thread.is_alive() for worker in runtime.workers))
            self.assertIsNone(mqtt_bridge._runtime)

    def test_failed_worker_start_releases_runtime_and_file_locks(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for name, value in {
                "BACKEND_ROOT": root, "MQTT_INBOX_PATH": root / "inbox.sqlite3",
                "MQTT_BRIDGE_ENABLED": True, "MQTT_BROKER_HOST": "test-broker",
                "_client": None, "_runtime": None,
            }.items():
                stack.enter_context(patch.object(mqtt_bridge, name, value))
            stack.enter_context(patch.object(mqtt_bridge.mqtt, "Client"))
            with patch.object(mqtt_bridge.InboxWorker, "start", side_effect=RuntimeError("cannot start")):
                with self.assertRaisesRegex(RuntimeError, "cannot start"):
                    mqtt_bridge.start_mqtt_bridge()
            self.assertIsNone(mqtt_bridge._runtime)
            owner = mqtt_bridge.ProcessLock((root / "inbox.sqlite3").with_suffix(".lock"))
            owner.close()

    def test_storage_recovery_restarts_network_loop_outside_callback(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            stack.enter_context(patch.object(mqtt_bridge, "BACKEND_ROOT", root))
            stack.enter_context(patch.object(mqtt_bridge, "MQTT_INBOX_PATH", root / "inbox.sqlite3"))
            client = MagicMock()
            runtime = mqtt_bridge._Runtime(client)
            try:
                runtime.pause_for_storage()
                client.connect_async.assert_not_called()
                with patch.object(runtime.stop_event, "wait", side_effect=[False, True]):
                    runtime._maintain()
                self.assertFalse(runtime.storage_failed.is_set())
                client.connect_async.assert_called_once()
                client.loop_start.assert_called_once()
            finally:
                runtime.close()

    def test_same_client_identity_cannot_use_two_local_inboxes(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            stack.enter_context(patch.object(mqtt_bridge, "BACKEND_ROOT", root))
            stack.enter_context(patch.object(mqtt_bridge, "MQTT_INBOX_PATH", root / "one.sqlite3"))
            runtime = mqtt_bridge._Runtime(MagicMock())
            try:
                with patch.object(mqtt_bridge, "MQTT_INBOX_PATH", root / "two.sqlite3"):
                    with self.assertRaises(RuntimeError):
                        mqtt_bridge._Runtime(MagicMock())
            finally:
                runtime.close()

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

        mqtt_bridge._deliver({"lane": "wave", "payload": message.payload, "event_id": None})

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
            mqtt_bridge._deliver({"lane": "wave", "payload": json.dumps(body).encode("utf-8"), "event_id": None})

        forwarded_ids = [call.args[1]["clientid"] for call in post_json.call_args_list]
        self.assertEqual(forwarded_ids, ["DTU-001", "DTU-002"])

    @patch.object(mqtt_bridge, "_post_json")
    def test_wave_message_without_clientid_is_rejected(self, post_json):
        message = SimpleNamespace(
            topic="backend/wave",
            payload=b'{"topic":"device/ac07e57d/up","payload_b64":"qgH+"}',
        )

        with self.assertRaisesRegex(ValueError, "missing clientid"):
            mqtt_bridge._message_data(message, "wave")

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

        mqtt_bridge._deliver({"lane": "gps", "payload": message.payload, "event_id": None})

        path, forwarded = post_json.call_args.args
        self.assertEqual(path, "/emqx/gps")
        self.assertEqual(forwarded, body)


if __name__ == "__main__":
    unittest.main()
