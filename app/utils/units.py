"""台账单位边界转换。"""

from decimal import Decimal


def metres_to_kilometres(value) -> str:
    """旧 cable_info 长度单位为米；managed_line 和前端台账使用千米。"""
    if value is None or value == "":
        return ""
    return format((Decimal(str(value)) / Decimal(1000)).normalize(), "f")
