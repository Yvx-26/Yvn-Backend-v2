"""集中装配所有 HTTP 路由。

业务模块只负责声明自己的 ``APIRouter``；应用入口仅引入这里导出的
``api_router``。这样新增、移除或调整模块时，不需要继续扩大 app.main。
"""

from fastapi import APIRouter

from app.api import (
    auth,
    cable,
    dashboard,
    device,
    emqx,
    infrastructure,
    location,
    waveform,
)


api_router = APIRouter()

api_router.include_router(emqx.router)
api_router.include_router(location.router)
api_router.include_router(waveform.router)
api_router.include_router(cable.router)
api_router.include_router(auth.router)
api_router.include_router(device.router)
api_router.include_router(dashboard.router)
api_router.include_router(infrastructure.router)
