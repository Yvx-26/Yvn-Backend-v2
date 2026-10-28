# 设备接入配置参考

部署新版可靠接收前，先阅读 [可靠接收说明](reliable-ingestion.md)，并人工完成接收回执迁移。
以下保留设备协议与 EMQX 规则示例；建议按可靠接收说明补充原始事件 `event_id`。

## 数据接入链路

```text
设备 / 定位模块
  -> DTU 4G
  -> EMQX
  -> EMQX 规则补充真实 clientid，并重新发布到后端专用 Topic
  -> 后端 MQTT Bridge 主动订阅 backend/wave、backend/gps
  -> 本地持久队列与消费线程
  -> POST /emqx/webhook（波形）或 POST /emqx/gps（定位）+ Idempotency-Key
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
