"""设备最新 GPS/BDS 定位查询接口。"""

from fastapi import APIRouter, Query, HTTPException
from app.core.config import TABLE_LOC
from app.db.mysql import get_conn

router = APIRouter()


# 接口 GET /api/location/latest：查询指定设备最近一次定位数据。
@router.get("/api/location/latest")
def location_latest(device_id: str = Query(...)):
    """返回设备最近一次定位；无记录时返回空坐标结构。"""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cursor:
            sql = f"SELECT device_id,lng,lat,ts,updated_at FROM {TABLE_LOC} WHERE device_id=%s LIMIT 1"
            cursor.execute(sql, (device_id,))
            row = cursor.fetchone()

        if not row:
            return {"device_id": device_id, "lng": None, "lat": None, "ts": None, "updated_at": None}
        return row
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()
