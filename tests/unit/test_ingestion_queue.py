import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.integrations import mqtt_bridge
from app.services.ingestion_queue import DurableInbox, InboxFull, ProcessLock
from app.services.ingestion_worker import InboxWorker, PermanentDeliveryError, process_one


class IngestionQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "inbox.sqlite3"
        self.inbox = DurableInbox(self.path)
        self.addCleanup(self.inbox.close)

    def enqueue(self, *, payload=None, mid=1, dup=False, lane="wave"):
        return self.inbox.enqueue("broker/client", lane, payload or b'{"clientid":"D1"}', mid=mid, dup=dup)

    def test_messages_survive_restart_and_inflight_is_recovered(self):
        event_id = self.enqueue()
        self.assertEqual(self.inbox.take("wave")["event_id"], event_id)
        self.inbox.close()
        reopened = DurableInbox(self.path)
        self.addCleanup(reopened.close)
        reopened.recover()
        self.assertEqual(reopened.take("wave")["event_id"], event_id)

    def test_mqtt_dup_reuses_event_but_reused_packet_id_for_new_publish_does_not(self):
        first = self.enqueue()
        self.assertEqual(first, self.enqueue(dup=True))
        self.assertNotEqual(first, self.enqueue(dup=False))
        self.assertEqual(self.inbox.counts(), {"pending": 2})

    def test_distinct_publishes_with_identical_content_are_not_dropped(self):
        self.assertNotEqual(self.enqueue(mid=1), self.enqueue(mid=2))

    def test_packet_mapping_cannot_confuse_wave_with_gps(self):
        first = self.enqueue(lane="wave")
        self.assertNotEqual(first, self.enqueue(lane="gps", dup=True))

    def test_overdue_retry_is_not_starved_by_new_messages(self):
        first = self.enqueue()
        event = self.inbox.take("wave")
        self.inbox.failed(event, RuntimeError("offline"))
        with patch("app.services.ingestion_queue.time.time", return_value=time.time() + 10):
            self.enqueue(mid=2)
            self.assertEqual(self.inbox.take("wave")["event_id"], first)

    def test_lane_recovery_does_not_requeue_other_inflight_lane(self):
        self.enqueue(lane="wave")
        self.enqueue(lane="gps", mid=2)
        self.inbox.take("wave")
        self.inbox.take("gps")
        self.inbox.recover("wave")
        self.assertIsNone(self.inbox.take("gps"))
        self.assertIsNotNone(self.inbox.take("wave"))

    def test_explicit_event_identifier_deduplicates_independent_of_packet_id(self):
        payload = b'{"clientid":"D1","event_id":"source-1"}'
        first = self.enqueue(payload=payload)
        second = self.enqueue(payload=b'{ "event_id": "source-1", "clientid": "D1" }', mid=2)
        self.assertEqual(first, second)
        self.assertEqual(self.inbox.counts(), {"pending": 1})

    def test_source_event_identity_does_not_depend_on_consumer_client_id(self):
        payload = b'{"clientid":"D1","event_id":"source-1"}'
        first = self.inbox.enqueue("broker/client-A", "wave", payload, mid=1, event_source="broker")
        second = self.inbox.enqueue("broker/client-B", "wave", payload, mid=2, event_source="broker")
        self.assertEqual(first, second)

    def test_reused_source_identity_with_other_data_is_quarantined(self):
        self.enqueue(payload=b'{"clientid":"D1","event_id":"1","value":1}')
        self.enqueue(payload=b'{"clientid":"D1","event_id":"1","value":2}', mid=2)
        self.assertEqual(self.inbox.counts(), {"pending": 1, "dead": 1})

    def test_new_broker_session_does_not_reuse_old_packet_mapping(self):
        first = self.enqueue()
        self.inbox.reset_packets("broker/client")
        self.assertNotEqual(first, self.enqueue(dup=True))

    def test_transient_failure_preserves_event_and_other_messages_can_proceed(self):
        first, second = self.enqueue(), self.enqueue(mid=2)
        process_one(self.inbox, "wave", MagicMock(side_effect=RuntimeError("DB offline")))
        later = self.inbox.take("wave")
        self.assertEqual(later["event_id"], second)
        self.inbox.complete(second)
        with patch("app.services.ingestion_queue.time.time", return_value=time.time() + 100):
            deliver = MagicMock()
            process_one(self.inbox, "wave", deliver)
        self.assertEqual(deliver.call_args.args[0]["event_id"], first)
        self.assertEqual(self.inbox.counts(), {"done": 2})

    def test_permanent_failure_is_retained_and_can_be_requeued(self):
        event_id = self.enqueue()
        process_one(self.inbox, "wave", MagicMock(side_effect=PermanentDeliveryError("invalid input")))
        self.assertEqual(self.inbox.counts(), {"dead": 1})
        self.inbox.requeue_dead()
        self.assertEqual(self.inbox.take("wave")["event_id"], event_id)

    def test_attempt_limit_does_not_delete_message(self):
        self.enqueue()
        process_one(self.inbox, "wave", MagicMock(side_effect=RuntimeError("offline")), max_attempts=1)
        self.assertEqual(self.inbox.counts(), {"dead": 1})
        row = self.inbox._db.execute("SELECT payload,attempts FROM inbox").fetchone()
        self.assertIsNotNone(row["payload"])
        self.assertEqual(row["attempts"], 1)

    def test_completed_payload_is_compacted_without_losing_duplicate_receipt(self):
        first = self.enqueue()
        process_one(self.inbox, "wave", lambda event: None)
        self.assertIsNone(self.inbox._db.execute("SELECT payload FROM inbox").fetchone()[0])
        self.assertEqual(first, self.enqueue(dup=True))
        self.assertIsNone(self.inbox.take("wave"))

    def test_capacity_limit_does_not_replace_accepted_messages(self):
        self.inbox.max_pending = 1
        self.enqueue()
        with self.assertRaises(InboxFull):
            self.enqueue(mid=2)
        self.assertEqual(self.inbox.counts(), {"pending": 1})

    def test_mqtt_ack_is_after_persist_and_callback_never_posts_http(self):
        client = MagicMock()
        client.ack.side_effect = lambda *_: self.assertEqual(self.inbox.counts(), {"pending": 1})
        runtime = SimpleNamespace(inbox=self.inbox, source="source", pause_for_storage=MagicMock())
        body = {"clientid": "D1", "topic": "device/ac07e57d/up", "payload_b64": "AA=="}
        msg = SimpleNamespace(topic=mqtt_bridge.MQTT_WAVE_TOPIC, payload=json.dumps(body).encode(), mid=1, qos=1, dup=False)
        with patch.object(mqtt_bridge, "_runtime", runtime), patch.object(mqtt_bridge.requests, "post") as post:
            mqtt_bridge._on_message(client, None, msg)
        client.ack.assert_called_once_with(1, 1)
        post.assert_not_called()

    def test_ack_failure_and_mqtt_redelivery_do_not_enqueue_twice(self):
        runtime = SimpleNamespace(inbox=self.inbox, source="source", pause_for_storage=MagicMock())
        body = {"clientid": "D1", "topic": "device/ac07e57d/up", "payload_b64": "AA=="}
        msg = SimpleNamespace(topic=mqtt_bridge.MQTT_WAVE_TOPIC, payload=json.dumps(body).encode(), mid=1, qos=1, dup=False)
        first = MagicMock()
        first.ack.side_effect = OSError("connection lost after disk commit")
        with patch.object(mqtt_bridge, "_runtime", runtime):
            mqtt_bridge._on_message(first, None, msg)
            msg.dup = True
            mqtt_bridge._on_message(MagicMock(), None, msg)
        self.assertEqual(self.inbox.counts(), {"pending": 1})

    def test_queue_uses_wal_and_full_synchronization(self):
        self.assertEqual(self.inbox._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.inbox._db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_operator_stats_are_read_only_and_retry_is_explicit(self):
        self.enqueue()
        process_one(self.inbox, "wave", MagicMock(side_effect=PermanentDeliveryError("invalid input")))
        command = [sys.executable, "-m", "app.services.ingestion_queue", "--path", str(self.path)]
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"dead": 1})
        self.assertEqual(self.inbox.counts(), {"dead": 1})
        result = subprocess.run(command + ["--retry-dead"], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"pending": 1})
        self.assertEqual(self.inbox.counts(), {"pending": 1})

    def test_storage_failure_does_not_ack_and_pauses_network(self):
        runtime = MagicMock()
        runtime.inbox.enqueue.side_effect = OSError("disk full")
        client = MagicMock()
        msg = SimpleNamespace(topic=mqtt_bridge.MQTT_GPS_TOPIC, payload=b'{}', mid=1, qos=1, dup=False)
        with patch.object(mqtt_bridge, "_runtime", runtime):
            mqtt_bridge._on_message(client, None, msg)
        client.ack.assert_not_called()
        runtime.pause_for_storage.assert_called_once()

    def test_poison_json_is_retained_before_ack(self):
        runtime = SimpleNamespace(inbox=self.inbox, source="source", pause_for_storage=MagicMock())
        client = MagicMock()
        msg = SimpleNamespace(topic=mqtt_bridge.MQTT_GPS_TOPIC, payload=b'not-json', mid=2, qos=1, dup=False)
        with patch.object(mqtt_bridge, "_runtime", runtime):
            mqtt_bridge._on_message(client, None, msg)
        self.assertEqual(self.inbox.counts(), {"dead": 1})
        client.ack.assert_called_once()

    def test_process_lock_prevents_two_bridge_owners(self):
        path = self.path.with_suffix(".lock")
        first = ProcessLock(path)
        try:
            with self.assertRaises(RuntimeError):
                ProcessLock(path)
        finally:
            first.close()
        ProcessLock(path).close()

    def test_slow_wave_worker_does_not_block_gps_worker(self):
        self.enqueue(lane="wave")
        self.enqueue(lane="gps", mid=2)
        release, started, gps_done = threading.Event(), threading.Event(), threading.Event()

        def wave(_):
            started.set()
            release.wait(2)

        workers = [InboxWorker(self.inbox, "wave", wave, lambda: None),
                   InboxWorker(self.inbox, "gps", lambda _: gps_done.set(), lambda: None)]
        for worker in workers:
            worker.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertTrue(gps_done.wait(1))
        finally:
            release.set()
            for worker in workers:
                worker.stop()
            for worker in workers:
                worker.join()

    def test_readiness_failure_does_not_consume_delivery_attempts(self):
        self.enqueue()
        called = threading.Event()

        def unavailable():
            called.set()
            raise RuntimeError("receipt migration missing")

        worker = InboxWorker(self.inbox, "wave", MagicMock(), unavailable)
        worker.start()
        try:
            self.assertTrue(called.wait(1))
        finally:
            worker.stop()
            worker.join()
        event = self.inbox.take("wave")
        self.assertEqual(event["attempts"], 1)
