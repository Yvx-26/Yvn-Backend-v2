"""按设备配置判断最近一次上报是否仍在在线期限内。"""

DEFAULT_OFFLINE_THRESHOLD_SECONDS = 90


def is_online(age_seconds, configured_threshold=None) -> bool:
    if age_seconds is None:
        return False

    threshold = DEFAULT_OFFLINE_THRESHOLD_SECONDS
    value = str(configured_threshold).strip().lstrip("0")
    if value.isascii() and value.isdecimal() and len(value) <= 16:
        seconds = int(value)
        if seconds <= 2**53 - 1:  # 与前端可精确表示的整数范围一致。
            threshold = seconds

    return int(age_seconds) <= threshold
