CREATE TABLE IF NOT EXISTS managed_line (
    line_id VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    line_type ENUM('submarine', 'land', 'hybrid') NOT NULL,
    length_m DECIMAL(15, 3) NULL,
    description TEXT NOT NULL,
    state ENUM('enabled', 'verification', 'draft') NOT NULL DEFAULT 'draft',
    payload_json JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (line_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS managed_device (
    device_id VARCHAR(64) NOT NULL,
    line_id VARCHAR(64) NOT NULL DEFAULT '',
    category ENUM('monitor', 'locator', 'integrated') NOT NULL,
    state ENUM('online', 'warning', 'offline') NOT NULL DEFAULT 'offline',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    payload_json JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (device_id),
    KEY idx_managed_device_line (line_id),
    KEY idx_managed_device_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS managed_line_device (
    line_id VARCHAR(64) NOT NULL,
    device_id VARCHAR(64) NOT NULL,
    sequence_no INT UNSIGNED NOT NULL,
    PRIMARY KEY (line_id, device_id),
    UNIQUE KEY uk_managed_line_sequence (line_id, sequence_no),
    CONSTRAINT fk_managed_line_device_line
        FOREIGN KEY (line_id) REFERENCES managed_line (line_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_managed_line_device_device
        FOREIGN KEY (device_id) REFERENCES managed_device (device_id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
