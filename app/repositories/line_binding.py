"""统一新旧资料的读取优先级；已建档设备的空绑定不得回退。"""

LINE_ID_SQL = "CASE WHEN m.device_id IS NOT NULL THEN NULLIF(m.line_id, '') ELSE b.cable_key END"
LINE_NAME_SQL = f"COALESCE(l.name, c.cable_name, {LINE_ID_SQL})"
LINE_LENGTH_M_SQL = "CASE WHEN l.line_id IS NOT NULL THEN l.length_m ELSE c.cable_length END"


def line_joins(device_column: str) -> str:
    """device_column 只能由内部 SQL 提供，不能接收用户输入。"""
    return f"""
        LEFT JOIN managed_device m ON m.device_id = {device_column} COLLATE utf8mb4_unicode_ci
        LEFT JOIN wave_device_bind b ON b.imei = {device_column} COLLATE utf8mb4_unicode_ci
        LEFT JOIN managed_line l ON l.line_id = ({LINE_ID_SQL}) COLLATE utf8mb4_unicode_ci
        LEFT JOIN cable_info c ON c.cable_key = ({LINE_ID_SQL}) COLLATE utf8mb4_unicode_ci
    """


def get_device_binding(cursor, device_id: str) -> dict:
    cursor.execute(
        f"""
        SELECT {LINE_ID_SQL} AS cable_key, {LINE_NAME_SQL} AS cable_name,
               {LINE_LENGTH_M_SQL} AS cable_length_m,
               CASE WHEN m.device_id IS NOT NULL THEN m.updated_at ELSE b.updated_at END AS updated_at
        FROM (SELECT %s AS device_id) d
        {line_joins('d.device_id')}
        """,
        (device_id,),
    )
    return cursor.fetchone() or {}


def get_bound_cable_info(cursor, imei: str):
    """
    根据设备 IMEI 查询绑定的电缆编号和电缆长度。

    已建档设备使用 managed_*，未建档时兼容旧绑定。
    managed_line.length_m 与旧 cable_info.cable_length 均为米，无需换算。

    返回：
        cable_key, cable_length_m
    """
    if not imei:
        return None, None

    row = get_device_binding(cursor, imei)
    length = row.get("cable_length_m")
    return row.get("cable_key"), float(length) if length is not None else None
