"""设备与电缆绑定关系接口。"""

from fastapi import APIRouter, Query, HTTPException

from app.core.config import TABLE_BIND
from app.db.mysql import get_conn
from app.schemas.device import WaveDeviceBindIn

router = APIRouter()


# 接口 GET /api/device_bind：查询指定设备 IMEI 当前绑定的电缆和地理位置信息。
@router.get("/api/device_bind")
def api_device_bind(imei: str = Query(...)):
    """查询设备当前绑定的电缆及可用定位信息。"""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cursor:
            sql = f"SELECT imei, cable_key, updated_at FROM {TABLE_BIND} WHERE imei=%s LIMIT 1"
            cursor.execute(sql, (imei,))
            row = cursor.fetchone()

        if not row:
            return {"imei": imei, "cable_key": ""}
        return row
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 接口 POST /api/device_bind：新增或更新设备与电缆、经纬度之间的绑定关系。
@router.post("/api/device_bind")
def api_save_device_bind(data: WaveDeviceBindIn):
    """新增或覆盖设备的电缆绑定关系。"""
    conn = None
    try:
        imei = (data.imei or "").strip()
        cable_key = (data.cable_key or "").strip()

        if not imei:
            raise HTTPException(status_code=400, detail="missing imei")
        if not cable_key:
            raise HTTPException(status_code=400, detail="missing cable_key")

        conn = get_conn()
        with conn.cursor() as cursor:
            sql = f"""
                INSERT INTO {TABLE_BIND} (imei, cable_key)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE
                    cable_key=VALUES(cable_key),
                    updated_at=CURRENT_TIMESTAMP
            """
            cursor.execute(sql, (imei, cable_key))
            conn.commit()

        return {"ok": True, "imei": imei, "cable_key": cable_key}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()
