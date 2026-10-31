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

DTU 4G -> EMQX -> MQTT Bridge -> 本地持久队列 -> 消费线程
  -> /emqx/webhook 或 /emqx/gps + Idempotency-Key -> MySQL 回执与采集数据
```

## 分层职责

- `app/main.py`：创建应用、配置跨域、挂载统一路由，并管理 MQTT Bridge 生命周期。
- `app/api/router.py`：唯一的运行时路由装配入口。
- `app/api/infrastructure.py`：线路和设备 HTTP 接口。
- `app/schemas/infrastructure.py`：与前端领域类型对齐的请求/响应模型。
- `app/services/infrastructure.py`：事务、资料持久化和绑定关系组装。
- `app/integrations/mqtt_bridge.py`：主动连接 EMQX、订阅 Topic、持久接收确认和网络生命周期。
- `app/services/ingestion_queue.py`：SQLite 持久队列、会话重发映射、进程锁与人工恢复工具。
- `app/services/ingestion_worker.py`：独立波形/定位消费、退避重试与失败隔离。
- `app/api/emqx.py`：HTTP JSON 读取、既有校验、线程卸载和错误响应，不执行入库 SQL。
- `app/services/ingestion.py`：原始帧、解析、定位与回执事务编排；业务错误由接口层映射为 HTTP 状态。
- `app/services/waveform.py`：最新、历史波形共用解码与响应投影；不同接口的筛选/排序规则分别保留。
- `app/services/dashboard.py`：大屏统计和显示状态组装。
- `app/services/device_inventory.py`：真实上报的设备发现与运行状态合并。
- `app/services/compat/`：历史纯波形、旧线路和旧配置适配；未做数据迁移前不删除这些行为。
- `app/protocols/tdr.py`：纯 4096 字节协议布局、校验和解析，不依赖 FastAPI、数据库或部署配置。
- `app/repositories/`：采集、大屏、波形、绑定、样本和回执 SQL，不提交或回滚业务事务。
- `app/utils/`：共享 Hex 清理、JSON/时间转换和长度单位转换。
- `app/db/mysql.py`：MySQL 连接工厂。
- `migrations/frontend_infrastructure.sql`：首批前端适配表结构。
- `migrations/latest_tdr_frame_protocol.sql`：把旧单相字段扩展为最新协议的相间字段。
- `migrations/ingestion_receipts.sql`：可靠接收所需回执表，需人工执行；本次尚未在当前库执行。

## 数据一致性

线路保存和设备顺序保存位于同一事务。线路或设备编号变更依赖外键的 `ON UPDATE CASCADE` 保持绑定表一致；响应所需的重查也在提交前完成，事务内失败会回滚。

已建档设备的所属线路以 `managed_device.line_id` 为准，空值表示明确解绑；只有未建档设备才回退旧 `wave_device_bind`。大屏、地图、运行态势和采集补充绑定共用该规则。旧资料接口在对象已建档时转交统一资料服务，不再形成第二套写入来源。

旧 `cable_info.cable_length`、新线路档案 `length_m`、接口 `lengthM` 和样本的 `cable_length_m` 均使用米。线路按节点顺序累加相邻坐标的球面距离，展示精度为 0.001 米（不代表定位精度）；地图连线是估算路径。增删、排序或编辑节点坐标后重新计算；缺失有效坐标时长度为空，不跨越缺失节点拼接路径。保存线路或设备资料时在同一事务内刷新线路长度；实时定位显示可随轮询刷新。历史样本长度不回写。

已有数据库升级前手动执行 `migrations/line_length_metres.sql`，将旧 `length_km` 乘以 1000 存入 `length_m` 并移除旧列及 JSON 字段；前后端需配套更新。新库直接使用新版基础资料建表文件。

线路写入推荐只提交节点 `{id, sequence}`；后端重读最新设备档案，不能通过线路排序覆盖设备参数。旧客户端的完整节点仍可接收，但已存在设备的属性以数据库档案为准。

设备的常用筛选字段独立成列，完整前端配置保存在 JSON 字段中。这样既能高效按线路、状态和设备类型筛选，也能在前端配置项逐步扩展时保持接口向后兼容。

## 本次内部路径调整

| 原位置 | 当前职责位置 |
| --- | --- |
| `services/tdr/parser.py` | 纯协议在 `protocols/tdr.py`，样本持久化在 `repositories/parsed_samples.py` |
| `services/mqtt_bridge.py` | `integrations/mqtt_bridge.py` |
| `services/line_binding.py`、`services/ingestion_receipts.py` | 对应移入 `repositories/`；幂等头格式校验保留在 HTTP 入口 |
| `api/emqx.py` 的事务与 SQL | 分别在 `services/ingestion.py` 和 `repositories/ingestion.py` |
| `api/dashboard.py`、`api/waveform.py` 的重复逻辑 | 对应业务服务与查询仓储；波形统一解码 |

旧位置不保留重复实现或额外转发壳。仓库内导入和测试已同步更新；外部脚本如果直接导入这些内部 Python 路径，也需更新。
HTTP 路径、请求参数、既有错误状态和响应字段保持不变。
基础资料的事务保存仍集中在 `services/infrastructure.py`，没有为了每条 SQL 机械拆分文件。

本次没有调整日期范围、采样截断、旧 CSV/Hex 回退或输入接受策略；性能优化与安全校验不混入结构调整。
