"""电缆基础信息的查询与保存接口。"""

from fastapi import APIRouter, Query, HTTPException
from app.db.mysql import get_conn
from app.schemas.cable import CableInfoIn
from app.schemas.infrastructure import ManagedLine
from app.services.infrastructure import list_lines, save_line
from app.utils.units import metres_to_kilometres

router = APIRouter()


# 接口 GET /api/cable_info：根据 cable_key 查询电缆名称、长度、备注及更新时间；不存在时返回空结构。
@router.get("/api/cable_info")
def get_cable_info(cable_key: str = Query(...)):
    """读取电缆名称、长度和备注，不存在时返回兼容前端的空结构。"""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT line_id AS cable_key, name AS cable_name, length_km * 1000 AS cable_length, "
                "description AS remark, updated_at FROM managed_line WHERE line_id=%s",
                (cable_key,),
            )
            managed = cursor.fetchone()
            if managed:
                return managed
            sql = """
            SELECT cable_key, cable_name, cable_length, remark, updated_at
            FROM cable_info
            WHERE cable_key=%s
            LIMIT 1
            """
            cursor.execute(sql, (cable_key,))
            row = cursor.fetchone()

        if not row:
            return {
                "cable_key": cable_key,
                "cable_name": "",
                "cable_length": None,
                "remark": "",
                "updated_at": None,
            }
        return row
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 接口 POST /api/cable_info：新增或更新指定 cable_key 的电缆基础信息。
@router.post("/api/cable_info")
def save_cable_info(data: CableInfoIn):
    """按 cable_key 新增或更新电缆基础信息。"""
    conn = None
    try:
        conn = get_conn()
        managed = next((item for item in list_lines(conn) if item["id"] == data.cable_key), None)
        if managed is not None:
            save_line(conn, ManagedLine.model_validate({
                **managed, "name": data.cable_name,
                "lengthKm": metres_to_kilometres(data.cable_length), "description": data.remark or "",
            }), data.cable_key)
            return {"status": "ok"}
        with conn.cursor() as cursor:
            # 构造带 ON DUPLICATE KEY UPDATE 的参数化 SQL，实现存在则更新、不存在则新增。
            sql = """
            INSERT INTO cable_info (cable_key, cable_name, cable_length, remark)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              cable_name=VALUES(cable_name),
              cable_length=VALUES(cable_length),
              remark=VALUES(remark)
            """
            cursor.execute(sql, (data.cable_key, data.cable_name, data.cable_length, data.remark))
            conn.commit()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()
