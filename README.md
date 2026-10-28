# 云平台 V2 后端

与 Frontend.v2 配套的设备数据接收、监测查询及线路设备管理后端。

## 技术框架

- Python 3.10+，FastAPI、Uvicorn、Pydantic 2。
- MySQL 8.0、PyMySQL。
- EMQX、paho-mqtt、Requests。
- Python 内置 SQLite：本地持久接收队列；python-dotenv：环境配置。
- 依赖版本以 requirements.txt 为准。

## 运行环境与配置

需要可访问的既有采集数据库；启用 MQTT 接收时还需 EMQX Broker。
仓库暂未提供旧采集表的完整基线，不能只靠现有迁移从空库建起全部功能。

首次安装：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在 .env 填写 MySQL、EMQX 与本地服务地址；现有 .env 不要覆盖或提交 Git。
系统环境变量优先于 .env，修改配置后需重启服务。

确认并人工执行所需迁移：

- migrations/frontend_infrastructure.sql：线路、设备资料及节点顺序。
- migrations/latest_tdr_frame_protocol.sql：最新相间协议字段。
- migrations/ingestion_receipts.sql：可靠接收回执。

**当前回执迁移仅已生成，尚未执行。新版 MQTT 可靠消费启用前必须先完成该迁移；缺表时消息保留在本地等待，不持续入库。**
本地 var/ 目录必须可写并持久保存，不能作为临时缓存清空。

```powershell
python main.py
python -m unittest discover -s tests -v
```

默认服务地址 http://127.0.0.1:8000，接口文档 /docs。
[接入配置](docs/device-ingestion.md)、[可靠接收与恢复](docs/reliable-ingestion.md)提供详细操作说明。

## 目录结构

```text
Backend.v2/
├─ main.py                 # 启动入口
├─ app/
│  ├─ api/                 # HTTP 接口与路由
│  ├─ services/            # 业务、事务、队列消费
│  │  └─ compat/           # 保留的历史数据适配
│  ├─ repositories/        # 查询与持久化 SQL
│  ├─ protocols/           # 纯设备协议解析
│  ├─ integrations/        # MQTT 等外部接入
│  ├─ schemas/             # 请求与数据模型
│  ├─ core/                # 配置、安全工具、业务异常
│  ├─ db/                  # 数据库连接
│  └─ utils/               # Hex、JSON、时间与单位转换
├─ migrations/             # 人工执行的数据库变更
├─ tests/                  # 单元、契约及内存回归测试
├─ docs/                   # 架构、接入与恢复说明
└─ var/                    # 运行时接收队列，不进入 Git
```

## 已实现功能

- 线路、设备档案及定位节点绑定、排序，兼容旧资料来源。
- 4096 字节设备帧解析：4000 点波形、10 组相间结果和设备上报长度。
- 最新/指定相间波形、历史帧分页、设备状态、定位和大屏汇总查询。
- MQTT 本地持久接收、断线重连、后台重试、回执去重及失败隔离；启用条件见上文。
- 最新定位更新及迟到位置防覆盖。
- 基础登录、会话查询、退出和接口文档；未新增业务接口鉴权。

独立告警业务、模型诊断、报表及角色权限尚未实现；接口安全与输入校验专项按当前安排暂缓。
