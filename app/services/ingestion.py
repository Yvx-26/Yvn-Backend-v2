"""设备采集业务：原始帧、解析、定位和回执事务；不接收 HTTP Request。"""

import math
import traceback

from app.core.config import WAVE_TOPICS, LOC_TOPICS
from app.core.errors import ServiceError
from app.db.mysql import get_conn
from app.protocols.tdr import normalize_phase_pair, get_frame_basic_info
from app.repositories.parsed_samples import insert_tdr_parsed_sample
from app.repositories.line_binding import get_bound_cable_info
from app.repositories import ingestion_receipts as receipts
from app.repositories import ingestion as storage
from app.services.payload import normalize_payload_to_hex, is_allowed_raw_length


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


def process_wave(data: dict, event_id: str | None = None):
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
            raise ServiceError(status_code=400, detail="missing clientid")

        # 定位 topic 不写入原始波形表。
        if topic in LOC_TOPICS:
            return {
                "status": "ignored",
                "ingestion_event_id": event_id,
                "reason": "location topic not stored in raw table",
                "topic": topic,
            }

        # 非波形 topic 直接忽略。
        if topic not in WAVE_TOPICS:
            print("[emqx_ignored] non-wave topic:", topic, flush=True)
            return {
                "status": "ignored",
                "ingestion_event_id": event_id,
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
            raise ServiceError(status_code=400, detail="missing payload_b64/payload_hex/payload")

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
        if not is_allowed_raw_length(decoded["length"]):
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
                "ingestion_event_id": event_id,
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
            receipt = receipts.claim(cursor, event_id, "wave", data)
            completed = receipts.completed_response(receipt)
            if completed is not None:
                conn.commit()
                return completed
            raw_frame_id = receipt.get("raw_frame_id") if receipt else None
            if raw_frame_id is None:
                raw_frame_id = storage.insert_raw_frame(
                    cursor, str(imei), phase_pair, decoded["hex"], decoded["length"],
                )

            # 查询刚刚插入的原始帧入库时间。
            row = storage.raw_frame_time(cursor, raw_frame_id)
            if row is None:
                raise RuntimeError("ingestion receipt refers to a missing raw frame")
            frame_created_at = row["CREATED_AT"] if row else None

            # 查询设备绑定的电缆编号和电缆长度。
            # cable_length_m 只保存后端绑定/线路台账长度；
            # 设备预留字段上报长度由 device_reported_cable_length_m 单独入库。
            context = receipts.pending_context(receipt)
            if "cable_key" in context:
                cable_key, cable_length_m = context["cable_key"], context.get("cable_length_m")
            else:
                cable_key, cable_length_m = get_bound_cable_info(cursor, str(imei))
            receipts.attach_raw(cursor, event_id, raw_frame_id, {
                "cable_key": cable_key, "cable_length_m": cable_length_m,
            })

            conn.commit()

        # ==============================
        # 第二步：尝试解析写入 tdr_parsed_sample
        # ==============================
        parsed_result = None
        parsed_error = None
        retryable = False
        parse_status = "skipped"

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

                    parse_status = "success"

            except Exception as parse_exc:
                # 解析失败不能影响原始数据。
                # 发生异常后回滚当前事务，避免部分写入造成数据不一致。
                conn.rollback()
                parsed_error = str(parse_exc)
                retryable = not isinstance(parse_exc, ValueError)
                parse_status = "pending" if retryable else "invalid"

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

        response = {
            "status": "ok",
            "ingestion_event_id": event_id,
            "parse_status": parse_status,
            "retryable": retryable,
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
        with conn.cursor() as cursor:
            receipts.finish(cursor, event_id, response, done=not retryable)
        # 解析结果与完成回执在同一事务；原始帧与 raw_frame_id 回执已在第一阶段原子保存。
        conn.commit()
        return response

    except ServiceError:
        if conn:
            conn.rollback()
        raise

    except Exception as e:
        if conn:
            conn.rollback()
        # 这里捕获的是整个接口的异常。
        # 如果走到这里，说明原始帧入库可能都失败了。
        print("[emqx_webhook_error]", repr(e), flush=True)
        print(traceback.format_exc(), flush=True)
        raise ServiceError(status_code=500, detail=str(e))

    finally:
        if conn:
            conn.close()


def process_gps(data: dict, event_id: str | None = None):
    """接收 EMQX 转发的 GPS / BDS 定位数据。

    定位数据只更新最新位置表，不写入 DEVICE_RAW_FRAME。前端地图接口会读取
    device_location_latest 表展示设备位置。
    """
    conn = None
    try:
        # 兼容不同字段命名，避免前端/设备端字段大小写差异导致解析失败。
        device_id = data.get("device_id") or data.get("deviceId") or data.get("clientid") or data.get("clientId")
        lng = data.get("lng")
        lat = data.get("lat")
        ts = data.get("ts")

        if device_id is None:
            raise ServiceError(status_code=400, detail="missing device_id")
        if lng is None or lat is None:
            raise ServiceError(status_code=400, detail="missing lng/lat")

        # 经纬度必须能转换为浮点数。
        try:
            lng = float(lng)
            lat = float(lat)
            if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
                raise ValueError("out of range")
        except Exception:
            raise ServiceError(status_code=400, detail="invalid lng/lat")

        # ts 为空或无法转换时，默认写 0。
        if ts is None:
            ts = 0
        try:
            ts = int(ts)
        except Exception:
            ts = 0

        conn = get_conn()
        with conn.cursor() as cursor:
            receipt = receipts.claim(cursor, event_id, "gps", data)
            completed = receipts.completed_response(receipt)
            if completed is not None:
                conn.commit()
                return completed
            # device_id 为唯一键时，重复上传只更新最新位置。
            changed = storage.upsert_location(cursor, str(device_id), lng, lat, ts)
            response = {"status": "ok", "ingestion_event_id": event_id, "location_updated": changed}
            receipts.finish(cursor, event_id, response)
            conn.commit()

        return response

    except ServiceError:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise ServiceError(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


def check_readiness():
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cursor:
            receipts.check_schema(cursor)
        return {"status": "ok", "idempotency": True}
    except Exception as exc:
        raise ServiceError(status_code=503, detail="接收回执未就绪，请检查数据库和 ingestion_receipts.sql 迁移") from exc
    finally:
        if conn:
            conn.close()
