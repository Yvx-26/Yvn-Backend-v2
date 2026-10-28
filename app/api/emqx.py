"""EMQX HTTP 入口：JSON 读取、既有校验、线程卸载与错误响应。"""

import json
import re

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.errors import ServiceError
from app.services import ingestion

router = APIRouter()


async def _read_json_body(request: Request) -> dict:
    """
    读取 EMQX HTTP Body。

    如果 EMQX 把二进制 payload 直接拼进 JSON，request.json() 会在 UTF-8 解码阶段失败。
    这里手动读取 body，并给出更明确的报错提示。
    """
    body = await request.body()

    try:
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        return data
    except UnicodeDecodeError as exc:
        print("[emqx_invalid_json_body] UnicodeDecodeError", repr(exc), flush=True)
        print("[emqx_invalid_json_body] first_120_bytes=", body[:120].hex(), flush=True)
        raise HTTPException(
            status_code=400,
            detail=(
                "EMQX webhook body is not valid UTF-8 JSON. "
                "Do not put binary MQTT payload directly into JSON. "
                "Use rule SQL: SELECT clientid, topic, base64_encode(payload) AS payload_b64 ... "
                "and HTTP body: {\"clientid\":\"${clientid}\",\"topic\":\"${topic}\",\"payload_b64\":\"${payload_b64}\"}."
            ),
        )
    except json.JSONDecodeError as exc:
        print("[emqx_invalid_json_body] JSONDecodeError", repr(exc), flush=True)
        print("[emqx_invalid_json_body] first_120_bytes=", body[:120].hex(), flush=True)
        raise HTTPException(status_code=400, detail="invalid JSON body from EMQX webhook")


def validate_event_id(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise HTTPException(status_code=400, detail="invalid Idempotency-Key")
    return value


async def _ingest(request: Request, processor):
    data = await _read_json_body(request)
    event_id = validate_event_id(request.headers.get("Idempotency-Key"))
    try:
        return await run_in_threadpool(processor, data, event_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/emqx/webhook")
async def emqx_webhook(request: Request):
    return await _ingest(request, ingestion.process_wave)


@router.post("/emqx/gps")
async def emqx_gps(request: Request):
    return await _ingest(request, ingestion.process_gps)


@router.get("/emqx/ingestion-health")
def ingestion_health():
    try:
        return ingestion.check_readiness()
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
