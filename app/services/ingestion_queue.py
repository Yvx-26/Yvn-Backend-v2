"""轻量持久化接收队列：先落盘、再确认，处理失败不丢弃原消息。"""

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class InboxFull(RuntimeError):
    pass


class DurableInbox:
    def __init__(self, path: Path, *, max_pending=10000, max_bytes=262144):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=2, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self.max_pending = max_pending
        self.max_bytes = max_bytes
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    def _initialize(self):
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, lane TEXT NOT NULL, payload BLOB,
                payload_hash TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS inbox_due ON inbox(lane,state,next_attempt);
            CREATE INDEX IF NOT EXISTS inbox_state_time ON inbox(state,updated_at);
            CREATE TABLE IF NOT EXISTS mqtt_packet (
                source TEXT NOT NULL, mid INTEGER NOT NULL, event_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, PRIMARY KEY(source,mid)
            );
            CREATE TABLE IF NOT EXISTS inbox_meta (key TEXT PRIMARY KEY,value TEXT);
        """)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def enqueue(self, source, lane, payload: bytes, *, mid=0, qos=1, dup=False, error=None, event_source=None):
        if len(payload) > self.max_bytes:
            raise InboxFull("MQTT message exceeds MQTT_MAX_MESSAGE_BYTES")
        digest = hashlib.sha256(lane.encode() + b"\0" + payload).hexdigest()
        event_id = None
        if not error:
            body = json.loads(payload)
            identity = body.get("event_id")
            if identity is None:
                identity = body.get("message_id")
            if isinstance(identity, (str, int)) and str(identity):
                publisher = body.get("clientid") or body.get("device_id") or ""
                event_id = hashlib.sha256(json.dumps(
                    [event_source or source, lane, publisher, str(identity)], separators=(",", ":"),
                ).encode()).hexdigest()
                digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        now = time.time()
        with self._transaction():
            if not event_id and qos and mid and dup:
                old = self._db.execute(
                    "SELECT event_id,payload_hash FROM mqtt_packet WHERE source=? AND mid=?",
                    (source, mid),
                ).fetchone()
                if old and old["payload_hash"] == digest:
                    event_id = old["event_id"]
            event_id = event_id or uuid.uuid4().hex
            existing = self._db.execute("SELECT payload_hash FROM inbox WHERE event_id=?", (event_id,)).fetchone()
            if existing and existing["payload_hash"] != digest:
                # 标识冲突需要人工检查；隔离保留，不能覆盖原消息或无限重试毒消息。
                event_id = uuid.uuid4().hex
                error = "source event identifier reused with different data"
                existing = None
            if not existing:
                count = self._db.execute("SELECT COUNT(*) FROM inbox WHERE state IN ('pending','processing','dead')").fetchone()[0]
                if count >= self.max_pending:
                    raise InboxFull("MQTT inbox is full; no acknowledgement was sent")
                self._db.execute(
                    "INSERT INTO inbox(event_id,lane,payload,payload_hash,state,last_error,created_at,updated_at,next_attempt) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (event_id, lane, payload, digest, "dead" if error else "pending", error, now, now, now),
                )
            if qos and mid:
                self._db.execute(
                    "INSERT INTO mqtt_packet(source,mid,event_id,payload_hash) VALUES (?,?,?,?) "
                    "ON CONFLICT(source,mid) DO UPDATE SET event_id=excluded.event_id,payload_hash=excluded.payload_hash",
                    (source, mid, event_id, digest),
                )
        return event_id

    def recover(self, lane=None):
        """只能由持有独占进程锁的启动流程调用。"""
        with self._transaction():
            if lane is None:
                self._db.execute("UPDATE inbox SET state='pending' WHERE state='processing'")
            else:
                self._db.execute("UPDATE inbox SET state='pending' WHERE state='processing' AND lane=?", (lane,))

    def reset_packets(self, source):
        with self._transaction():
            self._db.execute("DELETE FROM mqtt_packet WHERE source=?", (source,))

    def take(self, lane, *, now=None):
        now = time.time() if now is None else now
        with self._transaction():
            row = self._db.execute(
                "SELECT * FROM inbox WHERE lane=? AND state='pending' AND next_attempt<=? "
                "ORDER BY next_attempt,created_at LIMIT 1", (lane, now),
            ).fetchone()
            if not row:
                return None
            self._db.execute(
                "UPDATE inbox SET state='processing',attempts=attempts+1,updated_at=? WHERE event_id=?",
                (now, row["event_id"]),
            )
            return {**dict(row), "attempts": row["attempts"] + 1}

    def complete(self, event_id):
        with self._transaction():
            self._db.execute(
                "UPDATE inbox SET state='done',payload=NULL,last_error=NULL,updated_at=? WHERE event_id=?",
                (time.time(), event_id),
            )

    def failed(self, event, error, *, permanent=False, max_attempts=10):
        dead = permanent or event["attempts"] >= max_attempts
        delay = min(60, 2 ** min(event["attempts"] - 1, 6))
        with self._transaction():
            self._db.execute(
                "UPDATE inbox SET state=?,last_error=?,next_attempt=?,updated_at=? WHERE event_id=?",
                ("dead" if dead else "pending", str(error)[:2000], time.time() + delay, time.time(), event["event_id"]),
            )
        return dead

    def requeue_dead(self):
        with self._transaction():
            self._db.execute("UPDATE inbox SET state='pending',attempts=0,next_attempt=0 WHERE state='dead'")

    def check_writable(self):
        with self._transaction():
            count = self._db.execute("SELECT COUNT(*) FROM inbox WHERE state IN ('pending','processing','dead')").fetchone()[0]
            if count >= self.max_pending:
                raise InboxFull("MQTT inbox still full; wait for processing or operator recovery")
            self._db.execute(
                "INSERT INTO inbox_meta VALUES ('health',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )

    def prune_done(self, before):
        with self._transaction():
            self._db.execute("DELETE FROM inbox WHERE state='done' AND updated_at<?", (before,))

    def counts(self):
        with self._lock:
            return dict(self._db.execute("SELECT state,COUNT(*) FROM inbox GROUP BY state").fetchall())

    def close(self):
        with self._lock:
            self._db.close()


class ProcessLock:
    """操作系统文件锁，进程异常退出后自动释放，不依赖删除锁文件。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a+b")
        try:
            self._file.seek(0, 2)
            if self._file.tell() == 0:
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._file.close()
            raise RuntimeError("MQTT Bridge already running for this identity/inbox") from None

    def close(self):
        self._file.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="查看接收队列；停止 Bridge 后可重新排队隔离消息")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--retry-dead", action="store_true")
    args = parser.parse_args()
    path = args.path.resolve()
    if not path.is_file():
        parser.error("inbox file does not exist")
    if args.retry_dead:
        owner = ProcessLock(path.with_suffix(".lock"))
        try:
            inbox = DurableInbox(path)
            try:
                inbox.requeue_dead()
                print(json.dumps(inbox.counts()))
            finally:
                inbox.close()
        finally:
            owner.close()
    else:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            print(json.dumps(dict(connection.execute("SELECT state,COUNT(*) FROM inbox GROUP BY state").fetchall())))
