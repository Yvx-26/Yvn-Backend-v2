"""大屏 HTTP 路由；只保留参数处理、服务调用和连接生命周期。"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.db.mysql import get_conn
from app.protocols.tdr import PHASE_PAIR_ORDER
from app.services import dashboard as dashboard_service
from app.services import waveform as waveform_service

router = APIRouter()


def _query(action, *args):
    conn = get_conn()
    try:
        return action(conn, *args)
    finally:
        conn.close()


@router.get('/api/dashboard/summary')
def api_dashboard_summary():
    """返回大屏顶部使用的设备、波形、定位和告警汇总数。"""
    return _query(dashboard_service.summary)


@router.get('/api/dashboard/devices')
def api_dashboard_devices():
    """返回设备最新上报时间、定位和电缆绑定信息。"""
    return _query(dashboard_service.devices)


@router.get('/api/dashboard/wave/latest')
def api_dashboard_wave_latest(
    imei: str,
    phase_pair: Optional[str] = Query(None),
    include_phase_states: bool = False,
):
    """返回指定设备最新一帧，或指定相间最近一帧的 4000 点有效波形。"""
    selected_phase = phase_pair.strip().upper() if phase_pair is not None else None
    if selected_phase is not None and selected_phase not in PHASE_PAIR_ORDER:
        raise HTTPException(status_code=400, detail=f"invalid phase_pair: {phase_pair}")
    if include_phase_states:
        return _query(waveform_service.latest, imei, selected_phase, True)
    return _query(waveform_service.latest, imei, selected_phase)


@router.get('/api/dashboard/map')
def api_dashboard_map():
    """返回地图绘制所需的设备位置、电缆绑定和显示名称。"""
    return _query(dashboard_service.map_points)


@router.get('/api/dashboard/operation')
def api_dashboard_operation():
    """返回由真实原始帧汇总出的今日运行态势。"""
    return _query(dashboard_service.operation)
