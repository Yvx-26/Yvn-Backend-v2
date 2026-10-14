"""设备绑定请求模型。"""

from pydantic import BaseModel


class WaveDeviceBindIn(BaseModel):
    """将设备 IMEI 绑定到指定电缆。"""
    imei: str
    cable_key: str
