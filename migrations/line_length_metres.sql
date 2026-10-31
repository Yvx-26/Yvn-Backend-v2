-- 现有数据库升级：将线路档案长度列从千米转换为米。
-- 请先备份并停止旧后端，再手动执行。不会修改历史波形或解析样本。
-- 若中途失败可重新执行：只补齐空的新列，随后删除旧列，避免重复乘以 1000。
DELIMITER $$
DROP PROCEDURE IF EXISTS migrate_line_length_metres$$
CREATE PROCEDURE migrate_line_length_metres()
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='managed_line' AND COLUMN_NAME='length_km') THEN
        IF NOT EXISTS (SELECT 1 FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='managed_line' AND COLUMN_NAME='length_m') THEN
            ALTER TABLE managed_line ADD COLUMN length_m DECIMAL(15,3) NULL;
        END IF;
        UPDATE managed_line SET length_m=length_km * 1000 WHERE length_m IS NULL;
        ALTER TABLE managed_line DROP COLUMN length_km;
    END IF;
    UPDATE managed_line
    SET payload_json=JSON_SET(JSON_REMOVE(payload_json,'$.lengthKm'),
                              '$.lengthM',IFNULL(CAST(length_m AS CHAR),''));
END$$
CALL migrate_line_length_metres()$$
DROP PROCEDURE migrate_line_length_metres$$
DELIMITER ;
