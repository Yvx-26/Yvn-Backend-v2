"""基础资料的数据库 JSON 与时间显示转换，不访问外部系统。"""

import json
from datetime import datetime


def json_object(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str) and raw:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    return {}


def timestamp_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)
