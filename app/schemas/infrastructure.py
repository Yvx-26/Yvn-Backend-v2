"""线路与设备管理接口的数据契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DeviceNetworkConfig(StrictModel):
    accessMode: Literal["ethernet", "cellular", "fiber"]
    protocol: Literal["MQTT", "TCP", "HTTP"]
    ipAddress: str = ""
    serverPort: str = ""
    uploadTopic: str = ""
    heartbeatSeconds: str = ""
    retrySeconds: str = ""
    timeoutSeconds: str = ""
    offlineThresholdSeconds: str = ""


class DeviceAcquisitionConfig(StrictModel):
    batchSize: int = Field(ge=1)
    uploadMode: Literal["complete-batch", "sequential"]
    batchTimeoutSeconds: str = ""
    missingDataPolicy: Literal["mark-and-upload", "wait-for-completion"]
    sampleRate: str = ""
    triggerThreshold: str = ""
    offlineCacheBatches: str = ""
    backfillEnabled: bool = False


class ManagedDevice(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    category: Literal["monitor", "locator", "integrated"]
    lineId: str = Field(default="", max_length=64)
    position: str = Field(default="", max_length=255)
    coordinate: tuple[float, float]
    linkedLocatorId: str = Field(default="", max_length=64)
    state: Literal["online", "warning", "offline"] = "offline"
    enabled: bool = True
    lastSeen: str = ""
    latencyMs: int | None = Field(default=None, ge=0)
    signalDbm: int | None = None
    firmwareVersion: str = Field(default="", max_length=64)
    updatedAt: str = ""
    network: DeviceNetworkConfig
    acquisition: DeviceAcquisitionConfig | None = None


class BoundLineDevice(ManagedDevice):
    sequence: int = Field(ge=1)


class ManagedLine(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    type: Literal["submarine", "land", "hybrid"]
    lengthM: str = ""
    description: str = ""
    state: Literal["enabled", "verification", "draft"]
    updatedAt: str = ""
    devices: list[BoundLineDevice] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_membership(self):
        if len({item.id for item in self.devices}) != len(self.devices):
            raise ValueError("线路设备编号不能重复")
        if len({item.sequence for item in self.devices}) != len(self.devices):
            raise ValueError("线路设备顺序不能重复")
        return self


class LineDeviceReference(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=1)


class ManagedLineWrite(ManagedLine):
    # 兼容旧客户端的完整节点；新客户端只提交编号与顺序。
    devices: list[LineDeviceReference | BoundLineDevice] = Field(default_factory=list)
