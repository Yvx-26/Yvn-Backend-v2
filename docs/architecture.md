# 项目架构

## 当前运行边界

应用运行时只包含云平台业务路由、EMQX 数据入口和设备帧解析。模型推理、TDR 标注、采集批次治理和旧版静态网页已移除。

```text
Vue 前端
  -> /api/infrastructure
  -> FastAPI 路由
  -> Pydantic 契约校验
  -> infrastructure service
  -> MySQL

DTU 4G -> EMQX -> MQTT Bridge -> /emqx/webhook 或 /emqx/gps -> MySQL
```

## 分层职责

- `app/main.py`：创建应用、配置跨域、挂载统一路由，并管理 MQTT Bridge 生命周期。
- `app/api/router.py`：唯一的运行时路由装配入口。
- `app/api/infrastructure.py`：线路和设备 HTTP 接口。
- `app/schemas/infrastructure.py`：与前端领域类型对齐的请求/响应模型。
- `app/services/infrastructure.py`：事务、资料持久化和绑定关系组装。
- `app/services/mqtt_bridge.py`：主动连接 EMQX、订阅波形与定位 Topic、断线重连。
- `app/api/emqx.py`：设备波形和定位消息校验与入库。
- `app/db/mysql.py`：MySQL 连接工厂。
- `migrations/frontend_infrastructure.sql`：首批前端适配表结构。
- `migrations/latest_tdr_frame_protocol.sql`：把旧单相字段扩展为最新协议的相间字段。

## 数据一致性

线路保存和设备顺序保存位于同一事务。线路或设备编号变更依赖外键的 `ON UPDATE CASCADE` 保持绑定表一致；任何一步失败时整个事务回滚。

设备的常用筛选字段独立成列，完整前端配置保存在 JSON 字段中。这样既能高效按线路、状态和设备类型筛选，也能在前端配置项逐步扩展时保持接口向后兼容。
