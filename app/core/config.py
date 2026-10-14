"""项目路径、数据库连接、表名和设备 Topic 配置。"""

import os
from pathlib import Path

from dotenv import load_dotenv


# 自动读取后端根目录中的 .env，让新终端无需重复执行一组 $env: 命令。
# override=False 表示 PowerShell/部署平台显式注入的环境变量仍具有最高优先级。
BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_ROOT / ".env"
load_dotenv(dotenv_path=ENV_FILE, override=False, encoding="utf-8")

# MySQL；部署环境应通过环境变量覆盖默认值。
db_config = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    # 仓库中只保留无敏感信息的占位默认值；部署时必须通过环境变量注入真实密码。
    "password": os.getenv("DB_PASSWORD", "123456"),
    "database": os.getenv("DB_DATABASE", "emqx"),
    "charset": "utf8mb4",
}

# 数据表
TABLE_RAW = "DEVICE_RAW_FRAME"
TABLE_LOC = "device_location_latest"
TABLE_BIND = "wave_device_bind"
TABLE_TDR_PARSED_SAMPLE = "tdr_parsed_sample"

# EMQX Topic 与原始帧过滤
RAW_MIN_LEN = 1000
WAVE_TOPICS = {"device/ac07e57d/up", "device/ac07e57d/down"}
LOC_TOPICS = {"device/bds/up", "device/bds/down"}

# ============================================================
# 云端 EMQX MQTT 订阅配置
# ============================================================

# 是否启用“本地后端主动订阅云端 EMQX”。
# 云服务器旧部署时可以设为 false；
# 本地电脑运行后端时设为 true。
MQTT_BRIDGE_ENABLED = (
    os.getenv("MQTT_BRIDGE_ENABLED", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)

# 云服务器上 EMQX 的公网地址。
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "").strip()

# 普通 MQTT 默认端口。
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))

# 如果 EMQX 开启了用户名密码认证，在环境变量中填写。
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

# 本地 FastAPI 自己作为 MQTT Client 时使用的 Client ID。
# 绝对不要和 DTU 使用相同的 Client ID，否则可能把 DTU 顶下线。
MQTT_CLIENT_ID = os.getenv(
    "MQTT_CLIENT_ID",
    "tdr_local_backend"
).strip()

# EMQX 规则处理后重新发布给本地后端的 Topic。
# 本地后端不再直接订阅 DTU 原始 Topic。
MQTT_WAVE_TOPIC = os.getenv(
    "MQTT_WAVE_TOPIC",
    "backend/wave"
).strip()

MQTT_GPS_TOPIC = os.getenv(
    "MQTT_GPS_TOPIC",
    "backend/gps"
).strip()

# MQTT Bridge 收到数据后，继续调用你现有的 FastAPI 接口。
# Bridge 与 FastAPI 在同一个后端中运行，因此使用 127.0.0.1 即可。
MQTT_LOCAL_BACKEND_URL = os.getenv(
    "MQTT_LOCAL_BACKEND_URL",
    "http://127.0.0.1:8000"
).strip()

# 波形查询默认最小十六进制文本长度
DEFAULT_MIN_HEX_LEN = 6000
