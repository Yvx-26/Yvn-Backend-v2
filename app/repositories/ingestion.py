"""采集记录写入；调用方负责与接收回执共用同一事务。"""

from app.core.config import TABLE_RAW, TABLE_LOC


def insert_raw_frame(cursor, imei, phase_pair, payload, payload_len):
    cursor.execute(
        f"INSERT INTO {TABLE_RAW} (IMEI,CABLE,PAYLOAD,PAYLOAD_LEN) VALUES (%s,%s,%s,%s)",
        (imei, phase_pair, payload, payload_len),
    )
    return cursor.lastrowid


def raw_frame_time(cursor, raw_frame_id):
    cursor.execute(f"SELECT CREATED_AT FROM {TABLE_RAW} WHERE ID = %s", (raw_frame_id,))
    return cursor.fetchone()


def upsert_location(cursor, device_id, lng, lat, ts):
    sql = f"""
    INSERT INTO {TABLE_LOC} (device_id, lng, lat, ts)
    VALUES (%s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
      lng=IF(VALUES(ts)>COALESCE(ts,0) OR (VALUES(ts)=0 AND COALESCE(ts,0)=0),VALUES(lng),lng),
      lat=IF(VALUES(ts)>COALESCE(ts,0) OR (VALUES(ts)=0 AND COALESCE(ts,0)=0),VALUES(lat),lat),
      updated_at=IF(VALUES(ts)>COALESCE(ts,0) OR (VALUES(ts)=0 AND COALESCE(ts,0)=0),CURRENT_TIMESTAMP,updated_at),
      ts=IF(VALUES(ts)>COALESCE(ts,0) OR (VALUES(ts)=0 AND COALESCE(ts,0)=0),VALUES(ts),ts)
    """
    cursor.execute(sql, (device_id, lng, lat, ts))
    return cursor.rowcount > 0
