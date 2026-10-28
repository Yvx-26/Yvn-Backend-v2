"""接收回执与业务写入共用 MySQL 事务，不以波形内容猜测重复事件。"""

import hashlib
import json

from app.core.errors import ServiceError


def claim(cursor, event_id: str | None, kind: str, data: dict) -> dict | None:
    if event_id is None:
        return None
    digest = hashlib.sha256(json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    cursor.execute(
        "INSERT INTO ingestion_receipt (event_id,kind,payload_hash) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)",
        (event_id, kind, digest),
    )
    cursor.execute(
        "SELECT kind,payload_hash,raw_frame_id,status,response_json FROM ingestion_receipt "
        "WHERE event_id=%s FOR UPDATE", (event_id,),
    )
    row = cursor.fetchone()
    if row["kind"] != kind or row["payload_hash"] != digest:
        raise ServiceError(status_code=409, detail="Idempotency-Key reused with different data")
    return row


def completed_response(receipt: dict | None) -> dict | None:
    if not receipt or receipt["status"] != "done":
        return None
    value = receipt["response_json"]
    return value if isinstance(value, dict) else json.loads(value)


def pending_context(receipt: dict | None) -> dict:
    value = receipt.get("response_json") if receipt else None
    if not value:
        return {}
    return value if isinstance(value, dict) else json.loads(value)


def attach_raw(cursor, event_id: str | None, raw_frame_id: int, context: dict):
    if event_id:
        cursor.execute(
            "UPDATE ingestion_receipt SET raw_frame_id=%s,response_json=%s WHERE event_id=%s",
            (raw_frame_id, json.dumps(context, ensure_ascii=False, allow_nan=False), event_id),
        )


def finish(cursor, event_id: str | None, response: dict, *, done: bool = True):
    if event_id:
        cursor.execute(
            "UPDATE ingestion_receipt SET status=%s,response_json=%s WHERE event_id=%s AND status='pending'",
            ("done" if done else "pending", json.dumps(response, ensure_ascii=False, allow_nan=False), event_id),
        )


def check_schema(cursor):
    cursor.execute("SELECT event_id,kind,payload_hash,raw_frame_id,status,response_json FROM ingestion_receipt LIMIT 0")
