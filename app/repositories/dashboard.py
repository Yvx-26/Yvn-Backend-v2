"""大屏只读查询；筛选和排序保持原接口行为。"""

from app.repositories.line_binding import LINE_ID_SQL, LINE_NAME_SQL, line_joins

OFFLINE_THRESHOLD_SQL = "JSON_UNQUOTE(JSON_EXTRACT(m.payload_json, '$.network.offlineThresholdSeconds'))"


def summary(cursor):
    cursor.execute("SELECT COUNT(DISTINCT IMEI) AS device_total FROM DEVICE_RAW_FRAME")
    r1 = cursor.fetchone() or {}
    cursor.execute("SELECT COUNT(*) AS wave_today FROM DEVICE_RAW_FRAME WHERE DATE(CREATED_AT)=CURDATE()")
    r2 = cursor.fetchone() or {}
    cursor.execute("SELECT COUNT(*) AS location_today FROM device_location_latest")
    r3 = cursor.fetchone() or {}
    return r1, r2, r3


def device_rows(cursor):
    sql = f"""
    SELECT
        t.IMEI AS imei,
        {LINE_ID_SQL} AS cable_key,
        {LINE_NAME_SQL} AS cable_name,
        t.CREATED_AT AS last_wave_time,
        TIMESTAMPDIFF(SECOND, t.CREATED_AT, NOW()) AS age_seconds,
        {OFFLINE_THRESHOLD_SQL} AS offline_threshold_seconds
    FROM (
        SELECT IMEI, MAX(CREATED_AT) AS CREATED_AT
        FROM DEVICE_RAW_FRAME
        GROUP BY IMEI
    ) t
    {line_joins('t.IMEI')}
    ORDER BY t.CREATED_AT DESC
    """
    cursor.execute(sql)
    rows = cursor.fetchall() or []
    return rows


def map_rows(cursor):
    cursor.execute(f"""
        SELECT
            d.device_id AS imei,
            d.lng AS lng,
            d.lat AS lat,
            d.ts AS ts,
            d.updated_at AS updated_at,
            {LINE_ID_SQL} AS cable_key,
            {LINE_NAME_SQL} AS cable_name,
            TIMESTAMPDIFF(SECOND, d.updated_at, NOW()) AS age_seconds,
            {OFFLINE_THRESHOLD_SQL} AS offline_threshold_seconds
        FROM device_location_latest d
        {line_joins('d.device_id')}
        WHERE d.lng IS NOT NULL AND d.lat IS NOT NULL
        ORDER BY d.updated_at DESC
    """)
    rows = cursor.fetchall() or []
    return rows


def operation_totals(cursor):
    cursor.execute(
        """
        SELECT COUNT(*) AS total,
               MIN(CREATED_AT) AS first_time,
               MAX(CREATED_AT) AS last_time
        FROM DEVICE_RAW_FRAME
        WHERE DATE(CREATED_AT)=CURDATE()
        """
    )
    return cursor.fetchone() or {}


def receive_trend_rows(cursor):
    cursor.execute(
        """
        SELECT FLOOR(HOUR(CREATED_AT) / 2) * 2 AS bucket_hour, COUNT(*) AS frame_count
        FROM DEVICE_RAW_FRAME
        WHERE DATE(CREATED_AT)=CURDATE()
        GROUP BY bucket_hour
        ORDER BY bucket_hour
        """
    )
    return cursor.fetchall() or []


def line_volume_rows(cursor):
    cursor.execute(
        f"""
        SELECT COALESCE({LINE_NAME_SQL}, '未绑定线路') AS line_name,
               COALESCE({LINE_ID_SQL}, 'unbound') AS line_id,
               COUNT(*) AS frame_count
        FROM DEVICE_RAW_FRAME r
        {line_joins('r.IMEI')}
        WHERE DATE(r.CREATED_AT)=CURDATE()
        GROUP BY COALESCE({LINE_ID_SQL}, 'unbound'), COALESCE({LINE_NAME_SQL}, '未绑定线路')
        ORDER BY frame_count DESC
        """
    )
    return cursor.fetchall() or []


def recent_record_rows(cursor):
    cursor.execute(
        """
        SELECT ID, IMEI, CABLE, CREATED_AT
        FROM DEVICE_RAW_FRAME
        ORDER BY CREATED_AT DESC, ID DESC
        LIMIT 8
        """
    )
    return cursor.fetchall() or []


def wave_activity_rows(cursor):
    cursor.execute(
        f"""
        SELECT TIMESTAMPDIFF(SECOND, latest.CREATED_AT, NOW()) AS age_seconds,
               {OFFLINE_THRESHOLD_SQL} AS offline_threshold_seconds
        FROM (
            SELECT IMEI, MAX(CREATED_AT) AS CREATED_AT
            FROM DEVICE_RAW_FRAME
            GROUP BY IMEI
        ) latest
        LEFT JOIN managed_device m ON m.device_id = latest.IMEI COLLATE utf8mb4_unicode_ci
        """
    )
    return cursor.fetchall() or []
