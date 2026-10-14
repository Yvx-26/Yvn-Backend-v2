import unittest

from pydantic import ValidationError

from app.schemas.infrastructure import ManagedDevice, ManagedLine


def device_payload():
    return {
        "id": "ZT-01",
        "name": "一体化终端",
        "category": "integrated",
        "lineId": "LINE-A",
        "position": "接入点",
        "coordinate": [113.4, 34.8],
        "linkedLocatorId": "",
        "state": "online",
        "enabled": True,
        "lastSeen": "刚刚",
        "latencyMs": 12,
        "signalDbm": -55,
        "firmwareVersion": "v2.4.1",
        "updatedAt": "",
        "network": {
            "accessMode": "ethernet",
            "protocol": "MQTT",
            "ipAddress": "192.168.10.1",
            "serverPort": "1883",
            "uploadTopic": "device/zt-01/data",
            "heartbeatSeconds": "30",
            "retrySeconds": "5",
            "timeoutSeconds": "10",
            "offlineThresholdSeconds": "90",
        },
        "acquisition": None,
    }


class InfrastructureSchemaTests(unittest.TestCase):
    def test_frontend_device_contract_is_accepted(self):
        device = ManagedDevice.model_validate(device_payload())
        self.assertEqual(device.id, "ZT-01")
        self.assertEqual(device.coordinate, (113.4, 34.8))

    def test_line_accepts_ordered_bound_devices(self):
        line = ManagedLine.model_validate(
            {
                "id": "LINE-A",
                "name": "A 段",
                "type": "submarine",
                "lengthKm": "18.6",
                "description": "主干线路",
                "state": "enabled",
                "updatedAt": "",
                "devices": [{**device_payload(), "sequence": 1}],
            }
        )
        self.assertEqual(line.devices[0].sequence, 1)

    def test_unknown_device_state_is_rejected(self):
        payload = device_payload()
        payload["state"] = "broken"
        with self.assertRaises(ValidationError):
            ManagedDevice.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
