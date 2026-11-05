"""原始帧查询，保留历史接口和大屏接口不同的筛选、排序语义。"""

from app.core.config import TABLE_RAW
from app.protocols.tdr import PHASE_PAIR_ORDER


def find_latest_phase_frames(cursor, imei):
    """每个相间各取最近一帧；同秒上报按 ID 决定先后。只对最终十帧读取 payload。"""
    placeholders = ", ".join(["%s"] * len(PHASE_PAIR_ORDER))
    cursor.execute(f"""
        SELECT raw.ID, raw.IMEI, raw.CABLE, raw.PAYLOAD, raw.PAYLOAD_LEN, raw.CREATED_AT
        FROM {TABLE_RAW} raw
        JOIN (
            SELECT ID, ROW_NUMBER() OVER (
                PARTITION BY CABLE ORDER BY CREATED_AT DESC, ID DESC
            ) AS phase_rank
            FROM {TABLE_RAW}
            WHERE IMEI=%s AND CABLE IN ({placeholders})
        ) ranked ON ranked.ID=raw.ID AND ranked.phase_rank=1
    """, (imei, *PHASE_PAIR_ORDER))
    return cursor.fetchall()


def list_device_ids(cursor, limit):
    cursor.execute(f"""
        SELECT IMEI, MAX(ID) AS last_id
        FROM {TABLE_RAW}
        WHERE IMEI IS NOT NULL
          AND IMEI NOT IN ('None', 'sub_client', 'test_client')
        GROUP BY IMEI
        ORDER BY last_id DESC
        LIMIT %s
    """, (limit,))
    return cursor.fetchall()


def list_frame_rows(cursor, imei, limit, before_id, phase_pair=None):
    if phase_pair is not None:
        before_sql = " AND ID < %s" if before_id is not None else ""
        params = (imei, phase_pair, before_id, limit) if before_id is not None else (imei, phase_pair, limit)
        cursor.execute(f"""
            SELECT ID, IMEI, CABLE, PAYLOAD_LEN, CREATED_AT, PAYLOAD
            FROM {TABLE_RAW}
            WHERE IMEI=%s AND CABLE=%s{before_sql}
            ORDER BY ID DESC LIMIT %s
        """, params)
    elif before_id is not None:
        cursor.execute(f"""
            SELECT ID, IMEI, CABLE, PAYLOAD_LEN, CREATED_AT
            FROM {TABLE_RAW}
            WHERE IMEI=%s AND ID < %s
            ORDER BY ID DESC
            LIMIT %s
        """, (imei, before_id, limit))
    else:
        cursor.execute(f"""
            SELECT ID, IMEI, CABLE, PAYLOAD_LEN, CREATED_AT
            FROM {TABLE_RAW}
            WHERE IMEI=%s
            ORDER BY ID DESC
            LIMIT %s
        """, (imei, limit))
    return cursor.fetchall()


def find_history_frame(cursor, imei, frame_id, min_hex_len):
    if frame_id is not None:
        cursor.execute(f"""
            SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
            FROM {TABLE_RAW}
            WHERE IMEI=%s AND ID=%s
            LIMIT 1
        """, (imei, frame_id))
    else:
        cursor.execute(f"""
            SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
            FROM {TABLE_RAW}
            WHERE IMEI=%s AND PAYLOAD_LEN >= %s
            ORDER BY ID DESC
            LIMIT 1
        """, (imei, min_hex_len))
    return cursor.fetchone()


def find_latest_frame(cursor, imei, phase_pair):
    if phase_pair is None:
        cursor.execute("""
            SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
            FROM DEVICE_RAW_FRAME
            WHERE IMEI=%s
            ORDER BY CREATED_AT DESC, ID DESC
            LIMIT 1
        """, (imei,))
    else:
        cursor.execute("""
            SELECT ID, IMEI, CABLE, PAYLOAD, PAYLOAD_LEN, CREATED_AT
            FROM DEVICE_RAW_FRAME
            WHERE IMEI=%s AND CABLE=%s
            ORDER BY CREATED_AT DESC, ID DESC
            LIMIT 1
        """, (imei, phase_pair))
    return cursor.fetchone()
