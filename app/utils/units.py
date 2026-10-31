"""台账单位边界转换。"""

from decimal import Decimal


def format_metres(value) -> str:
    """新旧台账与接口统一使用米。"""
    if value is None or value == "":
        return ""
    return format(Decimal(str(value)).normalize(), "f")
