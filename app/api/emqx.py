"""EMQX Webhook 接收接口。
"""

import json
import traceback

from fastapi import APIRouter, Request, HTTPException

from app.core.config import TABLE_RAW, TABLE_LOC, WAVE_TOPICS, LOC_TOPICS
from app.db.mysql import get_conn
from app.services.payload import normalize_payload_to_hex, RAW_ALLOWED_LENS

# TDR 原始帧解析服务：
# 用于在 DEVICE_RAW_FRAME 保存成功后，
# 同步解析 4000 点波形并写入 tdr_parsed_sample。
from app.services.tdr.parser import (
    insert_tdr_parsed_sample,
    normalize_phase_pair,
    get_frame_basic_info,
)

router = APIRouter()


# 根据 IMEI 查询绑定线路和电缆长度，供 TDR 自动解析时补充样本信息。
# 注意：这里得到的是“后端绑定/线路台账长度”，不是设备帧头上报长度。
def get_bound_cable_info(cursor, imei: str):
    """
    根据设备 IMEI 查询绑定的电缆编号和电缆长度。

    数据来源：
    1. wave_device_bind：imei -> cable_key
    2. cable_info：cable_key -> cable_length

    返回：
        cable_key, cable_length_m
    """
    if not imei:
        return None, None

    cursor.execute(
        """
        SELECT cable_key
        FROM wave_device_bind
        WHERE imei = %s
        LIMIT 1
        """,
        (imei,),
    )
    bind_row = cursor.fetchone()

    if not bind_row:
        return None, None

    cable_key = bind_row.get("cable_key")

    if not cable_key:
        return None, None

    cursor.execute(
        """
        SELECT cable_length
        FROM cable_info
        WHERE cable_key = %s
        LIMIT 1
        """,
        (cable_key,),
    )
    cable_row = cursor.fetchone()

    cable_length_m = None
    if cable_row and cable_row.get("cable_length") is not None:
        cable_length_m = float(cable_row["cable_length"])

    return cable_key, cable_length_m


# 从 EMQX JSON 中选择实际 payload 字段，并标记其编码方式。
def _select_payload_field(data: dict):
    """兼容 EMQX Action 中不同 payload 字段命名。"""
    if "payload_b64" in data:
        return data.get("payload_b64"), "base64"
    if "payload_base64" in data:
        return data.get("payload_base64"), "base64"
    if "payload_hex" in data:
        return data.get("payload_hex"), "hex"
    if "payload" in data:
        # 旧代码使用 payload 字段。现在建议该字段中放 Base64 字符串，而不是二进制原文。
        return data.get("payload"), None
    return None, None


async def _read_json_body(request: Request) -> dict:
    """
    读取 EMQX HTTP Body。

    如果 EMQX 把二进制 payload 直接拼进 JSON，request.json() 会在 UTF-8 解码阶段失败。
    这里手动读取 body，并给出更明确的报错提示。
    """
    body = await request.body()

    try:
        return json.loads(body.decode("utf-8"))
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


# 接口 POST /emqx/webhook：处理波形上报：识别 Topic、规范化 payload、保存原始帧、解析 TDR 样本并输出采集进度。
@router.post("/emqx/webhook")
async def emqx_webhook(request: Request):
    """
    接收 EMQX 转发的波形数据。

    当前流程：
    1. 接收 EMQX Webhook JSON；
    2. 从 payload_b64 / payload_hex / payload 中取出波形 payload；
    3. 将 payload 统一转换成十六进制字符串；
    4. 从原始帧 Byte1 解析当前相间，优先作为 DEVICE_RAW_FRAME.CABLE；
    5. 保存原始帧到 DEVICE_RAW_FRAME；
    6. 保存成功后解析 4096 字节帧并写入 tdr_parsed_sample；
    """
    conn = None

    try:
        data = await _read_json_body(request)

        print(
            "[emqx_in]",
            "clientid=", data.get("clientid"),
            "device_imei=", data.get("device_imei"),
            "topic=", data.get("topic"),
            "keys=", list(data.keys()),
            flush=True
        )

        # EMQX Webhook 中通常使用 clientid 表示设备编号。
        imei = data.get("clientid") or data.get("clientId") or data.get("device_imei")

        # topic 用于判断当前消息是波形数据还是定位数据。
        topic = str(data.get("topic") or "").strip()

        if not imei:
            raise HTTPException(status_code=400, detail="missing clientid")

        # 定位 topic 不写入原始波形表。
        if topic in LOC_TOPICS:
            return {
                "status": "ignored",
                "reason": "location topic not stored in raw table",
                "topic": topic,
            }

        # 非波形 topic 直接忽略。
        if topic not in WAVE_TOPICS:
            print("[emqx_ignored] non-wave topic:", topic, flush=True)
            return {
                "status": "ignored",
                "reason": "non-wave topic",
                "topic": topic,
            }

        raw_payload, payload_encoding = _select_payload_field(data)

        print(
            "[emqx_payload_field]",
            "encoding=", payload_encoding,
            "payload_type=", type(raw_payload).__name__,
            "payload_len=", len(str(raw_payload or "")),
            flush=True
        )

        if raw_payload is None:
            raise HTTPException(status_code=400, detail="missing payload_b64/payload_hex/payload")

        # 将 EMQX payload 统一转换为十六进制字符串。
        # decoded["hex"] 是十六进制字符串；decoded["length"] 是十六进制字符长度。
        # 4096 字节原始帧对应 8192 个十六进制字符。
        decoded = normalize_payload_to_hex(raw_payload, encoding=payload_encoding)

        print(
            "[emqx_decoded]",
            "encoding=", decoded.get("encoding"),
            "hex_len=", decoded.get("length"),
            "byte_len=", decoded.get("byte_length"),
            "head=", decoded.get("hex", "")[:20],
            flush=True
        )

        # 保持原有逻辑：只有长度满足要求时才写入原始波形表。
        if decoded["length"] not in RAW_ALLOWED_LENS:
            print(
                "[emqx_ignored] payload length not allowed:",
                "topic=", topic,
                "hex_len=", decoded["length"],
                "byte_len=", decoded.get("byte_length"),
                "encoding=", decoded.get("encoding"),
                flush=True
            )
            return {
                "status": "ignored",
                "reason": "payload length not allowed for raw waveform table",
                "topic": topic,
                "payload_len": decoded["length"],
                "payload_encoding": decoded.get("encoding"),
            }

        # 从 4096 字节帧中读取当前相间，以及预留区 Byte4033~4034 中
        # 设备端设置的监测电缆长度。
        # 如果帧头异常，仍然先保存 DEVICE_RAW_FRAME，但 tdr_parsed_sample 会解析失败。
        frame_info = None
        frame_phase_pair = None
        device_reported_cable_length_m = None
        try:
            frame_info = get_frame_basic_info(decoded["hex"])
            # 只有帧长、帧头、帧尾和 Byte1 相间编码都有效，才信任帧内字段。
            if (
                frame_info.get("valid_length")
                and frame_info.get("valid_start")
                and frame_info.get("valid_end")
                and frame_info.get("phase_pair")
            ):
                frame_phase_pair = frame_info.get("phase_pair")
                # 设备在预留字段前两个字节上报的监测电缆长度单独保存到
                # tdr_parsed_sample.device_reported_cable_length_m。
                # 不再混入 cable_length_m，避免“绑定长度”和“设备上报长度”含义混淆。
                device_reported_cable_length_m = frame_info.get("device_reported_cable_length_m")
        except Exception as header_exc:
            print(f"[tdr_header_parse_failed] imei={imei}, error={header_exc}", flush=True)

        # 当前相间优先使用原始帧 Byte1；如果取不到，再兼容读取 HTTP JSON 附加字段。
        # 默认 A-B 只用于无效帧的原始数据标记，严格解析仍会拒绝该无效帧。
        phase_pair = normalize_phase_pair(
            frame_phase_pair
            or data.get("phase_pair")
            or data.get("cable")
            or data.get("phase")
            or "A-B"
        )

        conn = get_conn()

        # 默认先置空，避免没有绑定关系时报错。
        cable_key = None
        cable_length_m = None

        # ==============================
        # 第一步：先保存原始帧
        # ==============================
        with conn.cursor() as cursor:
            sql = f"""
            INSERT INTO {TABLE_RAW}
                (IMEI, CABLE, PAYLOAD, PAYLOAD_LEN)
            VALUES
                (%s, %s, %s, %s)
            """

            cursor.execute(
                sql,
                (
                    str(imei),
                    phase_pair,
                    decoded["hex"],
                    decoded["length"],
                ),
            )

            # 获取刚刚插入的 DEVICE_RAW_FRAME.ID。
            raw_frame_id = cursor.lastrowid

            print(
                "[raw_insert_ok]",
                "raw_frame_id=", raw_frame_id,
                "imei=", imei,
                "phase_pair=", phase_pair,
                "payload_len=", decoded["length"],
                flush=True,
            )

            # 查询刚刚插入的原始帧入库时间。
            cursor.execute(
                f"SELECT CREATED_AT FROM {TABLE_RAW} WHERE ID = %s",
                (raw_frame_id,),
            )
            row = cursor.fetchone()
            frame_created_at = row["CREATED_AT"] if row else None

            # 查询设备绑定的电缆编号和电缆长度。
            # cable_length_m 只保存后端绑定/线路台账长度；
            # 设备预留字段上报长度由 device_reported_cable_length_m 单独入库。
            cable_key, cable_length_m = get_bound_cable_info(cursor, str(imei))

            conn.commit()

        # ==============================
        # 第二步：尝试解析写入 tdr_parsed_sample
        # ==============================
        parsed_result = None
        parsed_error = None

        if decoded["length"] == 8192:
            try:
                with conn.cursor() as cursor:
                    parsed_result = insert_tdr_parsed_sample(
                        cursor=cursor,
                        raw_frame_id=raw_frame_id,
                        imei=str(imei),
                        phase_pair=phase_pair,
                        payload_hex=decoded["hex"],
                        payload_len=decoded["length"],
                        frame_created_at=frame_created_at,
                        cable_key=cable_key,
                        cable_length_m=cable_length_m,
                        device_reported_cable_length_m=device_reported_cable_length_m,
                    )

                    conn.commit()

            except Exception as parse_exc:
                # 解析失败不能影响原始数据。
                # 发生异常后回滚当前事务，避免部分写入造成数据不一致。
                conn.rollback()
                parsed_error = str(parse_exc)

                print(
                    f"[tdr_parse_failed] "
                    f"raw_frame_id={raw_frame_id}, "
                    f"imei={imei}, "
                    f"phase_pair={phase_pair}, "
                    f"payload_len={decoded['length']}, "
                    f"error={parsed_error}",
                    flush=True,
                )
        else:
            parsed_error = f"skip parse: payload_len={decoded['length']} is not 8192"

        return {
            "status": "ok",
            "topic": topic,
            "payload_len": decoded["length"],
            "payload_byte_len": decoded.get("byte_length"),
            "payload_encoding": decoded.get("encoding"),
            "raw_frame_id": raw_frame_id,
            # phase 保留为兼容字段；新前端应优先读取 phase_pair。
            "phase_pair": phase_pair,
            "phase": phase_pair,
            "frame_info": frame_info,
            "cable_key": cable_key,
            "cable_length_m": cable_length_m,
            "device_reported_cable_length_m": device_reported_cable_length_m,

            # parsed_sample_created = True 表示成功写入 tdr_parsed_sample。
            "parsed_sample_created": parsed_result is not None,

            # 成功时返回解析结果；失败时返回 None。
            "parsed_sample": parsed_result,

            # 解析失败或跳过解析时，这里会有原因。
            "parsed_error": parsed_error,
        }

    except HTTPException:
        raise

    except Exception as e:
        # 这里捕获的是整个接口的异常。
        # 如果走到这里，说明原始帧入库可能都失败了。
        print("[emqx_webhook_error]", repr(e), flush=True)
        print(traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        if conn:
            conn.close()


# 接口 POST /emqx/gps：处理 GPS/BDS 上报并更新设备最新定位表。
@router.post("/emqx/gps")
async def emqx_gps(request: Request):
    """接收 EMQX 转发的 GPS / BDS 定位数据。

    定位数据只更新最新位置表，不写入 DEVICE_RAW_FRAME。前端地图接口会读取
    device_location_latest 表展示设备位置。
    """
    conn = None
    try:
        data = await request.json()

        # 兼容不同字段命名，避免前端/设备端字段大小写差异导致解析失败。
        device_id = data.get("device_id") or data.get("deviceId") or data.get("clientid") or data.get("clientId")
        lng = data.get("lng")
        lat = data.get("lat")
        ts = data.get("ts")

        if device_id is None:
            raise HTTPException(status_code=400, detail="missing device_id")
        if lng is None or lat is None:
            raise HTTPException(status_code=400, detail="missing lng/lat")

        # 经纬度必须能转换为浮点数。
        try:
            lng = float(lng)
            lat = float(lat)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid lng/lat")

        # ts 为空或无法转换时，默认写 0。
        if ts is None:
            ts = 0
        try:
            ts = int(ts)
        except Exception:
            ts = 0

        conn = get_conn()
        with conn.cursor() as cursor:
            # device_id 为唯一键时，重复上传只更新最新位置。
            sql = f"""
            INSERT INTO {TABLE_LOC} (device_id, lng, lat, ts)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              lng=VALUES(lng),
              lat=VALUES(lat),
              ts=VALUES(ts),
              updated_at=CURRENT_TIMESTAMP
            """
            cursor.execute(sql, (str(device_id), lng, lat, ts))
            conn.commit()

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()
