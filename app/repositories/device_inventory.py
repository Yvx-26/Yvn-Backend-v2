"""从既有采集与定位表发现设备的只读查询。"""

def inventory_rows(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT IMEI AS device_id, MAX(CREATED_AT) AS last_wave_time
            FROM DEVICE_RAW_FRAME
            WHERE IMEI IS NOT NULL AND IMEI <> ''
            GROUP BY IMEI
            """
        )
        wave_rows = cursor.fetchall() or []
        cursor.execute(
            """
            SELECT device_id, lng, lat, updated_at
            FROM device_location_latest
            WHERE device_id IS NOT NULL AND device_id <> ''
            """
        )
        location_rows = cursor.fetchall() or []
        cursor.execute("SELECT imei, cable_key FROM wave_device_bind")
        binding_rows = cursor.fetchall() or []
    return wave_rows, location_rows, binding_rows
