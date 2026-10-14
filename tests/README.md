# 测试目录

- `unit/`：基础资料契约、MQTT 转发、Payload、TDR 帧协议和波形转换测试。
- 后续可增加 `integration/`，连接隔离测试库验证 Webhook、事务和各 API。

后续补充测试时，测试库必须与生产库使用不同的 `DB_DATABASE`。

当前全部纯逻辑测试可以直接运行：

```bash
python -m unittest discover -s tests -v
```
