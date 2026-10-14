"""电缆基础信息请求模型。"""

from typing import Optional
from pydantic import BaseModel


class CableInfoIn(BaseModel):
    """新增或更新电缆信息时使用的请求体。"""
    cable_key: str
    cable_name: str
    cable_length: Optional[float] = None
    remark: Optional[str] = None
