"""原始帧查询，保留历史接口和大屏接口不同的筛选、排序语义。"""

from app.core.config import TABLE_RAW


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


def list_frame_rows(cursor, imei, limit, before_id):
    if before_id is not None:
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
