"""原始帧列表与波形查询接口；查询与解码由公共波形服务完成。"""

from typing import Optional
from fastapi import APIRouter, Query, HTTPException

from app.core.config import DEFAULT_MIN_HEX_LEN
from app.protocols.tdr import PHASE_PAIR_ORDER
from app.db.mysql import get_conn
from app.services import waveform as waveform_service

router = APIRouter()


def _query(action, *args):
    conn = None
    try:
        conn = get_conn()
        return action(conn, *args)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if conn:
            conn.close()


@router.get("/api/devices")
def list_devices(limit: int = Query(200, ge=1, le=2000)):
    """按最近帧顺序返回原始帧表中出现过的设备 IMEI。"""
    return _query(waveform_service.list_device_ids, limit)


@router.get("/api/frames")
def list_frames(
    imei: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
    before_id: Optional[int] = Query(None),
    phase_pair: Optional[str] = None,
):
    """分页返回历史帧，可选相间筛选并附带该帧故障摘要。"""
    if phase_pair is not None:
        selected = phase_pair.strip().upper()
        if selected not in PHASE_PAIR_ORDER:
            raise HTTPException(status_code=400, detail=f"invalid phase_pair: {phase_pair}")
        return _query(waveform_service.list_frames, imei, limit, before_id, selected)
    return _query(waveform_service.list_frames, imei, limit, before_id)


@router.get("/api/waveform")
def waveform(
    imei: str = Query(...),
    frame_id: Optional[int] = Query(None),
    min_hex_len: int = Query(DEFAULT_MIN_HEX_LEN, ge=0, le=500000),
    max_points: int = Query(200000, ge=100, le=500000),
):
    """返回指定帧或最新有效帧，并同时提供完整包与 4000 点波形。"""
    return _query(waveform_service.history, imei, frame_id, min_hex_len, max_points)
