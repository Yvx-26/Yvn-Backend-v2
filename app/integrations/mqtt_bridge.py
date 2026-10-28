"""云端 EMQX -> 本地 FastAPI MQTT Bridge。

EMQX 已经通过规则完成：
1. 波形 payload Base64 编码；
2. 保留真实 DTU clientid；
3. GPS 字段提取；
4. 将处理后的数据重新发布到：
   backend/wave
   backend/gps

网络回调仅持久化接收日志并手动确认；后台线程转发、重试并隔离失败消息。
"""

import hashlib
import json
import logging
import threading
import time
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import requests

from app.core.config import (
    MQTT_BRIDGE_ENABLED,
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    MQTT_USERNAME,
    MQTT_PASSWORD,
    MQTT_CLIENT_ID,
    MQTT_WAVE_TOPIC,
    MQTT_GPS_TOPIC,
    MQTT_LOCAL_BACKEND_URL,
    BACKEND_ROOT,
    MQTT_INBOX_PATH,
    MQTT_INBOX_MAX_PENDING,
    MQTT_MAX_MESSAGE_BYTES,
    MQTT_RETRY_MAX_ATTEMPTS,
    MQTT_DONE_RETENTION_DAYS,
)
from app.services.ingestion_queue import DurableInbox, InboxFull, ProcessLock
from app.services.ingestion_worker import InboxWorker, PermanentDeliveryError


_client = None
_lock = threading.Lock()
_runtime = None
_ready_until = 0.0
_ready_url = ""
_ready_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _receiver_ready() -> None:
    global _ready_until, _ready_url
    url = MQTT_LOCAL_BACKEND_URL.rstrip('/')
    with _ready_lock:
        if _ready_url == url and time.monotonic() < _ready_until:
            return
        response = requests.get(f"{url}/emqx/ingestion-health", timeout=(3, 10))
        response.raise_for_status()
        if response.json().get("idempotency") is not True:
            raise RuntimeError("receiver does not support ingestion receipts")
        _ready_url, _ready_until = url, time.monotonic() + 10


def _post_json(path: str, data: dict, *, event_id: str | None = None) -> None:
    """把 EMQX 重发布的数据交给现有 FastAPI 接口。"""

    url = f"{MQTT_LOCAL_BACKEND_URL.rstrip('/')}{path}"

    response = requests.post(
        url,
        json=data,
        headers={"Idempotency-Key": event_id} if event_id else {},
        timeout=(3, 15),
    )
    if response.status_code in (400, 401, 403, 404, 409, 413, 422):
        raise PermanentDeliveryError(f"receiver rejected message: HTTP {response.status_code}")
    response.raise_for_status()
    result = response.json()
    if event_id and result.get("ingestion_event_id") != event_id:
        raise PermanentDeliveryError("receiver did not confirm the ingestion event identifier")
    if result.get("retryable"):
        raise RuntimeError("raw frame retained, parsed result pending; retrying same event")
    if result.get("status") != "ok" or result.get("parse_status") in ("invalid", "skipped"):
        raise PermanentDeliveryError("message was ignored or contains an invalid/unsupported frame")


def _decode_json_payload(msg) -> dict:
    """解析 EMQX 重新发布过来的 JSON 消息。"""

    text = msg.payload.decode("utf-8")

    data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError(
            f"MQTT payload is not JSON object: {msg.topic}"
        )

    return data


def _message_data(msg, lane):
    """网络接收与后台重试共用同一组既有字段检查。"""
    data = _decode_json_payload(msg)
    if lane == "wave":
        for key in ("clientid", "topic", "payload_b64"):
            if not data.get(key):
                label = "original topic" if key == "topic" else key
                raise ValueError(f"backend/wave missing {label}")
    elif lane == "gps":
        if not data.get("device_id"):
            raise ValueError("backend/gps missing device_id")
        for key in ("lng", "lat"):
            if data.get(key) is None:
                raise ValueError(f"backend/gps missing {key}")
    else:
        raise ValueError("unknown MQTT ingestion topic")
    json.dumps(data, allow_nan=False)
    return data


def _on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties,
):
    """连接成功后订阅 EMQX 重发布 Topic。"""

    if reason_code != 0:
        print(
            "[mqtt_bridge_connect_failed]",
            "reason=", reason_code,
            flush=True,
        )
        return

    if _runtime and not getattr(flags, "session_present", False):
        try:
            _runtime.inbox.reset_packets(_runtime.source)
        except Exception:
            logger.exception("unable to reset MQTT session receipt map")
            _runtime.pause_for_storage()
            return

    print(
        "[mqtt_bridge_connected]",
        f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}",
        flush=True,
    )

    # EMQX 重发布动作使用 QoS 1，
    # 本地订阅同样使用 QoS 1。
    client.subscribe(
        MQTT_WAVE_TOPIC,
        qos=1,
    )

    client.subscribe(
        MQTT_GPS_TOPIC,
        qos=1,
    )

    print(
        "[mqtt_bridge_subscribed]",
        MQTT_WAVE_TOPIC,
        MQTT_GPS_TOPIC,
        flush=True,
    )


def _on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties,
):
    print(
        "[mqtt_bridge_disconnected]",
        "reason=", reason_code,
        flush=True,
    )


def _on_message(
    client,
    userdata,
    msg,
):
    """落盘成功才 ACK，不在 MQTT 回调中请求 HTTP 或访问 MySQL。"""
    runtime = _runtime
    if runtime is None:
        return
    try:
        if len(msg.payload) > MQTT_MAX_MESSAGE_BYTES:
            raise InboxFull("MQTT message exceeds MQTT_MAX_MESSAGE_BYTES")
        lane = "wave" if msg.topic == MQTT_WAVE_TOPIC else "gps" if msg.topic == MQTT_GPS_TOPIC else "unknown"
        error = None
        try:
            _message_data(msg, lane)
        except (ValueError, UnicodeError, RecursionError) as exc:
            error = str(exc)
        runtime.inbox.enqueue(
            runtime.source, lane, bytes(msg.payload), mid=msg.mid, qos=msg.qos, dup=msg.dup, error=error,
            event_source=getattr(runtime, "event_source", runtime.source),
        )
        if msg.qos:
            client.ack(msg.mid, msg.qos)
        if error:
            logger.warning("invalid MQTT message retained in quarantine: %s", error)
    except Exception:
        logger.exception("MQTT message NOT acknowledged: local storage unavailable")
        runtime.pause_for_storage()


def _deliver(event):
    msg = SimpleNamespace(payload=event["payload"], event_id=event["event_id"], topic=event["lane"])
    try:
        data = _message_data(msg, event["lane"])
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PermanentDeliveryError("stored MQTT message is invalid") from exc
    path = "/emqx/webhook" if event["lane"] == "wave" else "/emqx/gps"
    try:
        _post_json(path, data, event_id=event["event_id"])
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("receiver returned invalid JSON; retrying the same event") from exc
    logger.debug("ingestion delivered lane=%s event=%s", event["lane"], event["event_id"])


class _Runtime:
    def __init__(self, client):
        if min(MQTT_INBOX_MAX_PENDING, MQTT_MAX_MESSAGE_BYTES, MQTT_RETRY_MAX_ATTEMPTS, MQTT_DONE_RETENTION_DAYS) < 1:
            raise RuntimeError("MQTT inbox/retry settings must be positive")
        if not MQTT_CLIENT_ID or not MQTT_WAVE_TOPIC or not MQTT_GPS_TOPIC or MQTT_WAVE_TOPIC == MQTT_GPS_TOPIC:
            raise RuntimeError("MQTT client ID and distinct wave/GPS topics are required")
        self.client = client
        self.event_source = f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}"
        self.source = f"{self.event_source}/{MQTT_CLIENT_ID}"
        identity = hashlib.sha256(self.source.encode()).hexdigest()[:24]
        self.identity_lock = ProcessLock(BACKEND_ROOT / "var" / f"mqtt-{identity}.lock")
        try:
            self.path_lock = ProcessLock(MQTT_INBOX_PATH.with_suffix(".lock"))
            try:
                self.inbox = DurableInbox(MQTT_INBOX_PATH, max_pending=MQTT_INBOX_MAX_PENDING, max_bytes=MQTT_MAX_MESSAGE_BYTES)
                self.inbox.recover()
            except BaseException:
                if hasattr(self, "inbox"):
                    self.inbox.close()
                self.path_lock.close()
                raise
        except BaseException:
            self.identity_lock.close()
            raise
        self.stop_event = threading.Event()
        self.storage_failed = threading.Event()
        self.workers = [InboxWorker(self.inbox, lane, _deliver, _receiver_ready, max_attempts=MQTT_RETRY_MAX_ATTEMPTS)
                        for lane in ("wave", "gps")]
        self.maintenance = threading.Thread(target=self._maintain, name="ingestion-maintenance", daemon=True)

    def start(self):
        for worker in self.workers:
            worker.start()
        self.maintenance.start()

    def pause_for_storage(self):
        self.storage_failed.set()
        self.client.disconnect()

    def _maintain(self):
        last_prune = 0.0
        last_error_log = 0.0
        while not self.stop_event.wait(2):
            try:
                if self.storage_failed.is_set():
                    # 恢复磁盘写入后重新启动网络循环，让持久会话重发未确认消息。
                    self.inbox.check_writable()
                    self.client.loop_stop()
                    if self.stop_event.is_set():
                        return
                    self.storage_failed.clear()
                    self.client.connect_async(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
                    self.client.loop_start()
                if time.monotonic() - last_prune >= 3600:
                    self.inbox.prune_done(time.time() - MQTT_DONE_RETENTION_DAYS * 86400)
                    last_prune = time.monotonic()
            except Exception:
                if time.monotonic() - last_error_log >= 30:
                    logger.exception("MQTT inbox maintenance failed; pending data retained")
                    last_error_log = time.monotonic()

    def close(self):
        self.stop_event.set()
        self.client.disconnect()
        if self.maintenance.ident is not None:
            self.maintenance.join(timeout=15)
        self.client.loop_stop()
        for worker in self.workers:
            worker.stop()
        for worker in self.workers:
            worker.join()
        if self.maintenance.is_alive():
            raise RuntimeError("MQTT maintenance thread did not stop")
        self.inbox.close()
        self.path_lock.close()
        self.identity_lock.close()


def start_mqtt_bridge() -> None:
    """启动 MQTT Bridge。"""

    global _client, _runtime

    if not MQTT_BRIDGE_ENABLED:

        print(
            "[mqtt_bridge] disabled",
            flush=True,
        )

        return

    if not MQTT_BROKER_HOST:

        raise RuntimeError(
            "MQTT_BRIDGE_ENABLED=true，"
            "但 MQTT_BROKER_HOST 未配置。"
        )

    with _lock:

        if _client is not None:
            return

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv311,
            clean_session=False,
            manual_ack=True,
        )

        if MQTT_USERNAME:

            client.username_pw_set(
                MQTT_USERNAME,
                MQTT_PASSWORD,
            )

        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message

        # 断网后自动重连。
        client.reconnect_delay_set(
            min_delay=1,
            max_delay=30,
        )

        runtime = _Runtime(client)
        _client, _runtime = client, runtime
        try:
            runtime.start()
            client.connect_async(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
            client.loop_start()
        except BaseException:
            runtime.close()
            _client = _runtime = None
            raise

        print(
            "[mqtt_bridge_connecting]",
            f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}",
            "client_id=", MQTT_CLIENT_ID,
            flush=True,
        )


def stop_mqtt_bridge() -> None:
    """FastAPI 停止时关闭 MQTT Client。"""

    global _client, _runtime

    with _lock:

        runtime = _runtime
        if runtime is None:
            return
        runtime.close()
        _client = _runtime = None
