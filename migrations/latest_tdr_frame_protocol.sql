-- 最新 4096 字节数据帧把旧“单相”字段改成 10 种“相间”字段。
-- 旧列名 phase 为兼容现有代码和历史数据保留，只扩展存储类型。
ALTER TABLE tdr_parsed_sample
    MODIFY COLUMN phase VARCHAR(8) NOT NULL
    COMMENT '当前相间：A-B/A-C/A-N/A-E/B-C/B-N/B-E/C-N/C-E/N-E';

-- DEVICE_RAW_FRAME.CABLE 同样保存当前相间；已有历史 A/B/C/N 数据不会被删除。
ALTER TABLE DEVICE_RAW_FRAME
    MODIFY COLUMN CABLE VARCHAR(32) DEFAULT 'A-B'
    COMMENT '数据帧当前相间，最新协议由 Byte1 解析';
