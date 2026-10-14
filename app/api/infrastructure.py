"""云平台前端的线路与设备管理接口。"""

from fastapi import APIRouter, HTTPException, Path

from app.db.mysql import get_conn
from app.schemas.infrastructure import ManagedDevice, ManagedLine
from app.services.infrastructure import get_snapshot, save_device, save_line


router = APIRouter(prefix="/api/infrastructure", tags=["infrastructure"])


@router.get("")
def api_infrastructure_snapshot():
    """一次返回设备与线路，确保前端拿到一致的绑定关系快照。"""
    conn = None
    try:
        conn = get_conn()
        return get_snapshot(conn)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="基础资料数据库暂不可用") from exc
    finally:
        if conn:
            conn.close()


@router.put("/devices/{device_id}")
def api_save_managed_device(
    data: ManagedDevice,
    device_id: str = Path(min_length=1, max_length=64),
):
    """新增或更新设备；路径参数表示编辑前的设备编号。"""
    conn = None
    try:
        conn = get_conn()
        return save_device(conn, data, device_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="设备保存失败") from exc
    finally:
        if conn:
            conn.close()


@router.put("/lines/{line_id}")
def api_save_managed_line(
    data: ManagedLine,
    line_id: str = Path(min_length=1, max_length=64),
):
    """新增或更新线路，同时保存设备绑定和线路顺序。"""
    conn = None
    try:
        conn = get_conn()
        return save_line(conn, data, line_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="线路保存失败") from exc
    finally:
        if conn:
            conn.close()


@router.get("/health")
def api_infrastructure_health():
    """供部署与前端联调检查服务是否已经启动。"""
    return {"status": "ok", "service": "infrastructure"}
