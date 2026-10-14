"""FastAPI 应用入口。
该文件只负责三件事：
1. 创建 FastAPI 应用；
2. 配置跨域；
3. 注册各业务路由。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.services.mqtt_bridge import (
    start_mqtt_bridge,
    stop_mqtt_bridge,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期。

    启动 FastAPI 时同步启动 MQTT Client；
    FastAPI 退出时关闭 MQTT Client。
    """

    start_mqtt_bridge()

    yield

    stop_mqtt_bridge()


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    app = FastAPI(
        title="Low Voltage Cable Fault Backend",
        lifespan=lifespan,
)

    # 允许前端跨域访问。当前前后端部署在同一服务器时影响不大，
    # 但开发阶段或外部页面访问接口时需要开启 CORS。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由装配统一放在 app.api.router，应用入口不再感知具体业务模块。
    app.include_router(api_router)

    return app


# 创建模块级 FastAPI 应用对象，供 uvicorn app.main:app 加载；根目录 main.py
# 继续提供 python main.py 和 uvicorn main:app 两种兼容启动方式。
app = create_app()
