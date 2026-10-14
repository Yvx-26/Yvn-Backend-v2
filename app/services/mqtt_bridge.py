"""云端 EMQX -> 本地 FastAPI MQTT Bridge。

EMQX 已经通过规则完成：
1. 波形 payload Base64 编码；
2. 保留真实 DTU clientid；
3. GPS 字段提取；
4. 将处理后的数据重新发布到：
   backend/wave
   backend/gps

本模块只负责：
1. 连接云端 EMQX；
2. 订阅 backend/wave 和 backend/gps；
3. 将收到的 JSON 原样转发给现有 FastAPI 接口。

因此：
- /emqx/webhook 无需修改；
- /emqx/gps 无需修改；
- 原有数据库和解析逻辑无需修改。
"""

import json
import threading

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
)


_client = None
_lock = threading.Lock()


def _post_json(path: str, data: dict) -> None:
    """把 EMQX 重发布的数据交给现有 FastAPI 接口。"""

    url = f"{MQTT_LOCAL_BACKEND_URL.rstrip('/')}{path}"

    response = requests.post(
        url,
        json=data,
        timeout=15,
    )

    response.raise_for_status()


def _decode_json_payload(msg) -> dict:
    """解析 EMQX 重新发布过来的 JSON 消息。"""

    text = msg.payload.decode("utf-8")

    data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError(
            f"MQTT payload is not JSON object: {msg.topic}"
        )

    return data


def _forward_wave(msg) -> None:
    """转发 backend/wave。

    EMQX 已经生成：

    {
        "clientid": "...",
        "topic": "device/ac07e57d/up",
        "payload_b64": "...",
        "timestamp": ...
    }
    """

    data = _decode_json_payload(msg)

    # 简单检查必要字段，避免错误消息进入数据库。
    if not data.get("clientid"):
        raise ValueError("backend/wave missing clientid")

    if not data.get("topic"):
        raise ValueError("backend/wave missing original topic")

    if not data.get("payload_b64"):
        raise ValueError("backend/wave missing payload_b64")

    # 原样交给现有 /emqx/webhook。
    _post_json(
        "/emqx/webhook",
        data,
    )

    print(
        "[mqtt_bridge_wave_ok]",
        "clientid=", data.get("clientid"),
        "topic=", data.get("topic"),
        flush=True,
    )


def _forward_gps(msg) -> None:
    """转发 backend/gps。

    EMQX 已经生成：

    {
        "device_id": "...",
        "lng": ...,
        "lat": ...,
        "ts": ...
    }
    """

    data = _decode_json_payload(msg)

    if not data.get("device_id"):
        raise ValueError("backend/gps missing device_id")

    if data.get("lng") is None:
        raise ValueError("backend/gps missing lng")

    if data.get("lat") is None:
        raise ValueError("backend/gps missing lat")

    # 原样交给现有 /emqx/gps。
    _post_json(
        "/emqx/gps",
        data,
    )

    print(
        "[mqtt_bridge_gps_ok]",
        "device_id=", data.get("device_id"),
        "lng=", data.get("lng"),
        "lat=", data.get("lat"),
        flush=True,
    )


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
    """根据 EMQX 重发布 Topic 分流。"""

    try:

        if msg.topic == MQTT_WAVE_TOPIC:

            _forward_wave(msg)

        elif msg.topic == MQTT_GPS_TOPIC:

            _forward_gps(msg)

        else:

            print(
                "[mqtt_bridge_ignored]",
                "topic=", msg.topic,
                flush=True,
            )

    except Exception as exc:

        # 单条消息异常不能使 MQTT 客户端退出。
        print(
            "[mqtt_bridge_forward_failed]",
            "topic=", msg.topic,
            "error=", repr(exc),
            flush=True,
        )


def start_mqtt_bridge() -> None:
    """启动 MQTT Bridge。"""

    global _client

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

        client.connect_async(
            MQTT_BROKER_HOST,
            MQTT_BROKER_PORT,
            keepalive=60,
        )

        client.loop_start()

        _client = client

        print(
            "[mqtt_bridge_connecting]",
            f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}",
            "client_id=", MQTT_CLIENT_ID,
            flush=True,
        )


def stop_mqtt_bridge() -> None:
    """FastAPI 停止时关闭 MQTT Client。"""

    global _client

    with _lock:

        client = _client
        _client = None

    if client is None:
        return

    try:

        client.disconnect()

    finally:

        client.loop_stop()

        print(
            "[mqtt_bridge] stopped",
            flush=True,
        )