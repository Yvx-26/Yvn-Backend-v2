# 云平台 V2 后端

这是与 `Frontend.v2` 配套的 FastAPI 后端。后端主动订阅 EMQX，将 DTU 4G 上报的设备波形和 GPS/BDS 定位数据保存到 MySQL，同时为云平台前端提供业务管理接口。当前不启用模型推理。

## 当前范围

已启用：

- 登录与登录态校验。
- MQTT 主动订阅、断线重连、设备波形与定位消息入库。
- 线路档案、设备档案、线路设备绑定及顺序管理。
- 原后端的只读设备、波形、定位和大屏查询接口，便于后续逐页适配。
- MySQL 持久化、接口文档和前端开发代理。

已从当前工程移除：

- TDR 人工标注、采集批次及质量检查管理接口。
- PI-WaveMTL 模型加载、推理和 AI 诊断接口。
- 旧版静态 HTML 页面。

运行依赖不再包含 PyTorch、NumPy 等模型依赖，只保留 MQTT 和 HTTP 转发所需的轻量依赖。

## 技术栈

- Python 3.10+
- FastAPI
- MySQL 8.0
- PyMySQL
- Pydantic 2

## 快速启动

创建环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

首次运行时复制环境配置模板：

```powershell
Copy-Item .env.example .env
```

然后只需在 `.env` 中填写一次真实的 MySQL 和 EMQX 连接信息。后端每次启动
都会自动读取该文件，新开终端后不再需要重复执行 `$env:` 命令。系统环境变量、
PowerShell `$env:` 和部署平台注入的同名配置优先级高于 `.env`，仍可用于临时覆盖。

`.env` 已加入 `.gitignore`，不要把真实密码写入 `.env.example` 或提交到仓库。

在目标数据库执行 `migrations/frontend_infrastructure.sql` 和
`migrations/latest_tdr_frame_protocol.sql`，然后启动服务：

```powershell
python main.py
```

修改 `.env` 后需要重启后端进程才能生效。

服务地址为 `http://127.0.0.1:8000`，接口文档为 `http://127.0.0.1:8000/docs`。

## 数据接入链路

```text
设备 / 定位模块
  -> DTU 4G
  -> EMQX
  -> EMQX 规则补充真实 clientid，并重新发布到后端专用 Topic
  -> 后端 MQTT Bridge 主动订阅 backend/wave、backend/gps
  -> POST /emqx/webhook（波形）或 POST /emqx/gps（定位）
  -> MySQL
  -> 前端监控与地图查询接口
```

后端 MQTT Client ID 必须与任何 DTU Client ID 不同。普通 MQTT 订阅者看不到原发布者的 Client ID，因此由 EMQX 规则在消息仍处于 Broker 内部时读取 `clientid`，连同原 Topic 和 Base64 波形一起重新发布。后端不再使用固定的 `MQTT_SOURCE_CLIENTID`，多台 DTU 即使向同一个原始 Topic 上报，也能按各自的 Client ID 入库。

## 最新波形数据帧

后端严格按 4096 字节完整帧解析：Byte0 为 `0xAA`，Byte1 为 10 种相间
编码，Byte2 当前不解析，Byte3~22 为 10 组故障距离，Byte23~32 为 10 组
故障类型，Byte33~4032 为 4000 个原始波形字节。预留区 Byte4033~4034
按 uint16 大端序保存设备端设置的监测电缆长度（单位 m），Byte4095 为 `0x55`。

波形 API 返回原始 `0~255` 字节，前端绘图时按 uint8 转 int8：`0~127`
保持不变，`128~255` 减去 256。接口同时返回 `phase_pair`、
`interphase_results`、当前故障类型和距离，前端不再自行解析协议偏移。

## EMQX 规则配置

需要在 EMQX 中建立两条规则和对应的“消息重新发布”动作。后端只订阅处理后的 Topic，不直接订阅 DTU 原始 Topic。

波形规则读取原始波形 Topic：

```sql
SELECT clientid, topic, timestamp, base64_encode(payload) AS payload_b64
FROM "device/ac07e57d/+"
```

将结果以 QoS 1 重新发布到 `backend/wave`，消息体必须是下面的 JSON 对象：

```json
{
  "clientid": "${clientid}",
  "topic": "${topic}",
  "timestamp": ${timestamp},
  "payload_b64": "${payload_b64}"
}
```

其中 `clientid` 必须直接使用 EMQX 规则事件中的发布者 Client ID，不能写成固定值。

定位规则读取 `device/bds/+`。将原始 JSON 中的设备编号、经纬度和时间整理后，以 QoS 1 重新发布到 `backend/gps`。后端期望的消息体为：

```json
{
  "device_id": "GPS-01",
  "lng": 113.5,
  "lat": 34.8,
  "ts": 1725000000000
}
```

如果定位模块的原始 JSON 使用 `deviceId`，请在 EMQX 规则中映射成 `device_id`。不同 EMQX 版本的规则动作界面名称可能略有差异，但最终重新发布的 Topic 和 JSON 字段应保持一致。

## 已接通的前端接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/infrastructure` | 一次获取线路、设备和绑定顺序 |
| PUT | `/api/infrastructure/devices/{device_id}` | 新增或更新设备，支持修改编号 |
| PUT | `/api/infrastructure/lines/{line_id}` | 新增或更新线路及设备顺序 |
| GET | `/api/infrastructure/health` | 联调健康检查 |
| POST | `/emqx/webhook` | 保存 EMQX 波形消息与解析样本 |
| POST | `/emqx/gps` | 更新设备最新定位 |

线路与设备请求/响应字段直接对应前端 `ManagedLine` 和 `ManagedDevice` 类型。前端开发服务会把 `/api` 代理到 `http://127.0.0.1:8000`；分开部署时，可通过 `VITE_API_BASE_URL` 指定后端地址。

## 根目录职责

| 目录或文件 | 作用 | 是否可直接删除 |
| --- | --- | --- |
| `app/` | FastAPI 应用全部运行时代码 | 不可删除 |
| `.venv/` | 当前项目的 Python 虚拟环境和已安装依赖 | 可通过重新创建虚拟环境恢复，但运行前必须存在可用环境 |
| `docs/` | 架构边界、分层和数据流说明 | 不影响运行，建议保留 |
| `migrations/` | 需要人工确认并执行的 MySQL 表结构变更 | 不可随意删除或重复执行 |
| `tests/` | Payload、MQTT、协议、波形和接口契约单元测试 | 不进入生产运行，建议保留 |
| `main.py` | 兼容 `python main.py` 和 `uvicorn main:app` 的启动入口 | 不可删除 |
| `requirements.txt` | Python 运行依赖及固定版本 | 不可删除 |
| `.env.example` | 数据库和 MQTT 环境变量示例，不包含真实密码 | 建议保留 |
| `.gitignore` | Git 忽略规则 | 建议保留 |

## `app/` 目录职责

| 目录或文件 | 作用 |
| --- | --- |
| `main.py` | 创建 FastAPI、配置跨域、注册统一路由，并随应用启动/停止 MQTT Bridge |
| `api/` | HTTP 接口层；只处理请求、响应和接口级校验 |
| `api/router.py` | 所有业务路由的唯一装配入口 |
| `api/emqx.py` | EMQX 波形和定位数据入口及原始帧入库 |
| `api/dashboard.py` | 首页、运行态势、地图和最新/指定相间波形查询 |
| `api/infrastructure.py` | 前端线路与设备资料统一管理接口 |
| `api/auth.py` | 登录、登录态查询和退出 |
| `api/cable.py`、`api/device.py` | 兼容的电缆和设备绑定接口 |
| `api/location.py`、`api/waveform.py` | 定位、历史帧和波形查询接口 |
| `core/` | 数据库、MQTT、Topic、表名等配置，以及登录令牌安全工具 |
| `db/` | PyMySQL 连接工厂 |
| `schemas/` | Pydantic 请求模型和接口数据契约 |
| `services/` | 可被多个接口复用的业务、持久化和数据转换逻辑 |
| `services/tdr/` | 固定 4096 字节设备帧解析和 `tdr_parsed_sample` 入库逻辑 |
| `services/mqtt_bridge.py` | 主动连接 EMQX、订阅后端 Topic、断线重连并转交 HTTP 入库接口 |
| `services/infrastructure.py` | 线路、设备、绑定顺序的读取与事务保存 |
| `services/payload.py`、`services/waveform.py` | Base64/Hex 归一化和波形字节转换 |
| `__init__.py` | 明确 Python 包边界，供导入和测试工具识别 |

运行后自动生成的 `__pycache__/` 和 `*.pyc` 只是 Python 字节码缓存，可以随时删除，不属于源码。

## 其他业务目录

| 目录 | 内容 |
| --- | --- |
| `migrations/` | `frontend_infrastructure.sql` 创建前端线路/设备资料表；`latest_tdr_frame_protocol.sql` 扩展最新相间帧字段 |
| `tests/unit/` | 不连接真实生产库的纯逻辑单元测试 |
| `docs/` | 项目整体架构和数据一致性说明 |

## 验证

```powershell
python -m unittest discover -s tests -v
cd ..\Frontend.v2
npm run type-check
npm run build
```
