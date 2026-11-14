from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from plankton_usb_agent import ownership
from plankton_usb_agent.ownership import (
    OwnershipError,
    OwnershipUnavailable,
    UsbOwnership,
)


@dataclass(frozen=True)
class Device:
    device_id: str = "device-1"
    block_device: str = "/dev/sda1"
    uuid: str = "uuid-1"
    serial: str = "serial-1"


def _claim_result(**overrides: object) -> dict[str, object]:
    return {
        "ok": True,
        "owned_root": "/media/plankton-usb/uuid-1",
        "lease_id": "lease-1",
        "generation": "4",
        "consumer_id": "plankton-usb",
        "native_user": "operator",
        **overrides,
    }


def test_ownership_uses_one_canonical_runtime_module_identity() -> None:
    assert ownership.__name__ == "plankton_usb_agent.ownership"
    assert ownership.OwnershipLease.__module__ == "plankton_usb_agent.ownership"
    assert ownership.UsbOwnership.__module__ == "plankton_usb_agent.ownership"
    assert not any(name.startswith("host.usb_agent.plankton_usb_agent") for name in sys.modules)


def test_claim_returns_one_identity_bound_root_and_release_reuses_lease() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def broker(operation: str, _device: Device, **values: object) -> dict[str, object]:
        calls.append((operation, values))
        return _claim_result() if operation == "claim" else {"ok": True, "released": True}

    owned = UsbOwnership(broker, lambda _device, _lease: True)
    lease = owned.claim(Device())

    assert lease.owned_root == Path("/media/plankton-usb/uuid-1")
    assert lease.native_user == "operator"
    assert lease.generation == 4
    assert not hasattr(lease, "native_root")
    assert owned.release(Device(), lease)
    assert calls == [
        ("claim", {"consumer_id": "plankton-usb"}),
        (
            "release",
            {
                "consumer_id": "plankton-usb",
                "owned_root": Path("/media/plankton-usb/uuid-1"),
                "lease_id": "lease-1",
                "generation": "4",
            },
        ),
    ]


@pytest.mark.parametrize("detail", ["native mount is busy", "device is already owned"])
def test_claim_reports_retryable_exclusive_ownership_conflicts(detail: str) -> None:
    def broker(_operation: str, _device: Device, **_values: object) -> dict[str, object]:
        raise RuntimeError(detail)

    with pytest.raises(OwnershipUnavailable, match=detail):
        UsbOwnership(broker, lambda _device, _lease: True).claim(Device())


def test_claim_reconciles_and_rejects_when_consumer_view_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[str] = []
    monkeypatch.setattr("plankton_usb_agent.ownership.time.sleep", lambda _delay: None)

    def broker(operation: str, _device: Device, **_values: object) -> dict[str, object]:
        operations.append(operation)
        return _claim_result(released=operation == "release")

    with pytest.raises(OwnershipError, match="controlled USB view"):
        UsbOwnership(broker, lambda _device, _lease: False).claim(Device())

    assert operations == ["claim", "release"]


def test_claim_allows_a_transient_consumer_view_to_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    views = iter((False, True))
    monkeypatch.setattr("plankton_usb_agent.ownership.time.sleep", lambda _delay: None)

    lease = UsbOwnership(
        lambda _operation, _device, **_values: _claim_result(),
        lambda _device, _lease: next(views),
    ).claim(Device())

    assert lease.lease_id == "lease-1"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"owned_root": "relative"}, "invalid ownership path"),
        ({"owned_root": "/media/operator/uuid-1"}, "invalid ownership path"),
        ({"lease_id": ""}, "invalid ownership lease"),
        ({"generation": "0"}, "invalid ownership lease"),
        ({"consumer_id": "other"}, "invalid ownership lease"),
    ],
)
def test_claim_rejects_untrusted_broker_lease(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(OwnershipError, match=message):
        UsbOwnership(
            lambda _operation, _device, **_values: _claim_result(**overrides),
            lambda _device, _lease: True,
        ).claim(Device())


def test_release_rejects_lease_for_a_replaced_device() -> None:
    owned = UsbOwnership(
        lambda operation, _device, **_values: (
            _claim_result() if operation == "claim" else {"ok": True, "released": True}
        ),
        lambda _device, _lease: True,
    )
    lease = owned.claim(Device())

    with pytest.raises(OwnershipError, match="stale ownership lease"):
        owned.release(Device(serial="replacement"), lease)


def test_release_requires_consumer_view_to_disappear() -> None:
    owned = UsbOwnership(
        lambda operation, _device, **_values: (
            _claim_result() if operation == "claim" else {"ok": True, "released": True}
        ),
        lambda _device, _lease: True,
        verify_released_view=lambda _device, _lease: False,
    )
    lease = owned.claim(Device())

    with pytest.raises(OwnershipError, match="controlled USB view did not release"):
        owned.release(Device(), lease)


def test_security_eject_uses_the_same_identity_bound_lease() -> None:
    calls: list[str] = []

    def broker(operation: str, _device: Device, **_values: object) -> dict[str, object]:
        calls.append(operation)
        return _claim_result() if operation == "claim" else {"ok": True, "released": True}

    owned = UsbOwnership(broker, lambda _device, _lease: True)
    lease = owned.claim(Device())

    assert owned.eject(Device(), lease)
    assert calls == ["claim", "eject"]
