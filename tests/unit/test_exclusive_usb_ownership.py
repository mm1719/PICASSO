from dataclasses import dataclass
from pathlib import Path

from plankton_usb_agent.ownership import UsbOwnership


@dataclass(frozen=True)
class Device:
    device_id: str = "device"
    block_device: str = "/dev/sda1"
    uuid: str = "uuid"
    serial: str = "serial"


def test_claim_release_uses_one_verified_owned_root() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def broker(operation: str, _device: Device, **values: object) -> dict[str, object]:
        calls.append((operation, values))
        return {
            "ok": True,
            "owned_root": "/media/plankton-usb/uuid",
            "lease_id": "lease-1",
            "generation": "1",
            "consumer_id": "plankton-usb",
        }

    ownership = UsbOwnership(broker, lambda _device, lease: lease.owned_root.is_absolute())
    device = Device()
    lease = ownership.claim(device)
    assert lease.owned_root == Path("/media/plankton-usb/uuid")
    assert not hasattr(lease, "native_root")
    assert ownership.release(device, lease)
    assert [operation for operation, _ in calls] == ["claim", "release"]
    assert "owned_root" in calls[-1][1]
