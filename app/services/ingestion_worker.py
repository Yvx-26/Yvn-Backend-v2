"""持久化接收队列的独立消费线程；重试不会占用 MQTT 网络线程。"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


class PermanentDeliveryError(RuntimeError):
    pass


def process_one(inbox, lane, deliver, *, max_attempts=10):
    event = inbox.take(lane)
    if event is None:
        return False
    try:
        deliver(event)
        inbox.complete(event["event_id"])
    except Exception as exc:
        dead = inbox.failed(
            event, exc, permanent=isinstance(exc, PermanentDeliveryError), max_attempts=max_attempts,
        )
        logger.warning("ingestion delivery %s event=%s attempt=%s error=%s",
                       "quarantined" if dead else "retry scheduled", event["event_id"], event["attempts"], exc)
    return True


class InboxWorker:
    def __init__(self, inbox, lane, deliver, ready, *, max_attempts=10):
        self.inbox, self.lane, self.deliver, self.ready = inbox, lane, deliver, ready
        self.max_attempts = max_attempts
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"ingestion-{lane}", daemon=True)

    def _run(self):
        last_error_log = 0.0
        recovery_needed = False
        while not self.stop_event.is_set():
            try:
                self.ready()
                if recovery_needed:
                    self.inbox.recover(self.lane)
                    recovery_needed = False
                if process_one(self.inbox, self.lane, self.deliver, max_attempts=self.max_attempts):
                    continue
                self.stop_event.wait(0.2)
            except Exception:
                recovery_needed = True
                # 缺表、数据库不可用或本地队列故障不消耗消息的重试次数。
                if time.monotonic() - last_error_log >= 30:
                    logger.exception("ingestion worker unavailable lane=%s; messages retained", self.lane)
                    last_error_log = time.monotonic()
                self.stop_event.wait(3)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def join(self):
        if self.thread.ident is None:
            return
        self.thread.join(timeout=25)
        if self.thread.is_alive():
            raise RuntimeError(f"ingestion worker did not stop: {self.lane}")
