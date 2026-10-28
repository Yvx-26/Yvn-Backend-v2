# 数据库迁移

本目录用于保存可重复执行、按版本排序的数据库结构变更。

项目当前依赖以下表，但原始项目未提供建表定义：

- `admin_user`
- `DEVICE_RAW_FRAME`
- `device_location_latest`
- `wave_device_bind`
- `cable_info`
- `tdr_parsed_sample`
- `managed_line`（由 `frontend_infrastructure.sql` 新增）
- `managed_device`（由 `frontend_infrastructure.sql` 新增）
- `managed_line_device`（由 `frontend_infrastructure.sql` 新增）

在未从实际 MySQL 导出并核对字段、索引、唯一键和外键前，不在这里猜测生成基线
SQL。建议从现有生产/测试库执行 `SHOW CREATE TABLE`，确认后保存为
`001_initial_schema.sql`，后续变更按 `002_*.sql`、`003_*.sql` 递增。

`frontend_infrastructure.sql` 是当前运行范围必须执行的迁移，用于线路档案、
设备档案和线路设备排序。后端不会在启动或请求过程中自动执行建表语句。

`latest_tdr_frame_protocol.sql` 将旧单相枚举字段扩展为相间字符串，必须在
启用最新 4096 字节数据帧解析前执行。该迁移保留现有历史记录，不会清空数据。

模型推理迁移已经移除。当前新环境只需在既有采集库基础上执行
`frontend_infrastructure.sql` 和 `latest_tdr_frame_protocol.sql`。

## 可靠接收回执（第二批改进）

`ingestion_receipts.sql` 只新增 `ingestion_receipt`。在启用新版 MQTT Bridge 或
HTTP `Idempotency-Key` 前由操作者人工执行；后端不会自动建表。
该表必须与原始帧、定位、解析样本位于同一个支持事务的 MySQL 库中。

当前仅生成迁移文件，按用户要求暂不对当前数据库执行。缺表时可靠消费暂停、
消息保留在本地队列，不会把未确认处理成功的数据直接当作完成。
完整启用和恢复步骤见 `docs/reliable-ingestion.md`。
