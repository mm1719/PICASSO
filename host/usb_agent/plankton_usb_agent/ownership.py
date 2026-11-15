"""Single ownership seam for an exclusive USB lease."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DeviceIdentity(Protocol):
    @property
    def device_id(self) -> str: ...
    @property
    def block_device(self) -> str: ...
    @property
    def uuid(self) -> str: ...
    @property
    def serial(self) -> str: ...


BrokerCall = Callable[..., dict[str, object]]
ViewVerifier = Callable[..., bool]


@dataclass(frozen=True, slots=True)
class OwnershipLease:
    device_id: str
    filesystem_uuid: str
    hardware_serial: str
    consumer_id: str
    owned_root: Path
    lease_id: str
    generation: int
    native_user: str | None = None


class OwnershipError(RuntimeError):
    pass


class OwnershipUnavailable(OwnershipError):
    pass


class UsbOwnership:
    """Deep ownership implementation; callers only receive verified leases."""

    def __init__(
        self,
        broker: BrokerCall,
        verify_view: ViewVerifier,
        *,
        verify_released_view: ViewVerifier | None = None,
        owned_root_parent: Path | None = Path("/media/plankton-usb"),
    ) -> None:
        self._broker = broker
        self._verify_view = verify_view
        self._verify_released_view = verify_released_view
        self._owned_root_parent = owned_root_parent
        self._leases: dict[str, OwnershipLease] = {}

    def _consumer_view_converged(self, device: DeviceIdentity, lease: OwnershipLease) -> bool:
        for attempt in range(50):
            if self._verify_view(device, lease):
                return True
            if attempt < 49:
                time.sleep(0.1)
        return False

    def claim(self, device: DeviceIdentity, *, native_user: str | None = None) -> OwnershipLease:
        values: dict[str, str] = {"consumer_id": "plankton-usb"}
        try:
            result = self._broker("claim", device, **values)
        except Exception as error:
            if str(error) in {"native mount is busy", "device is already owned"}:
                raise OwnershipUnavailable(str(error)) from error
            raise
        lease = self._lease_from_result(device, result, self._owned_root_parent, native_user)
        if not self._consumer_view_converged(device, lease):
            try:
                self.release(device, lease)
            except (OSError, RuntimeError):
                pass
            raise OwnershipError("controlled USB view is unavailable")
        self._leases[device.device_id] = lease
        return lease

    def release(
        self,
        device: DeviceIdentity,
        lease: OwnershipLease | None = None,
    ) -> bool:
        lease = lease or self._leases.get(device.device_id)
        values: dict[str, str | Path] = {"consumer_id": "plankton-usb"}
        if lease is not None:
            self._check_lease(device, lease)
            values.update(
                owned_root=lease.owned_root,
                lease_id=lease.lease_id,
                generation=str(lease.generation),
            )
        # A lost claim reply leaves cleanup authority only in the broker ledger.
        result = self._broker("release" if lease is not None else "reconcile", device, **values)
        if result.get("retained"):
            raise OwnershipUnavailable("USB ownership belongs to an active job")
        released = bool(result.get("released", lease is not None))
        released_view_verified = (
            lease is not None
            and self._verify_released_view is not None
            and self._verify_released_view(device, lease)
        )
        if (
            lease is not None
            and self._verify_released_view is not None
            and not released_view_verified
        ):
            raise OwnershipError("controlled USB view did not release")
        if released_view_verified:
            released = True
        if released:
            self._leases.pop(device.device_id, None)
        return released

    def bind_job(self, device: DeviceIdentity, job_id: str, lease: OwnershipLease) -> None:
        self._check_lease(device, lease)
        self._broker(
            "bind_job",
            device,
            consumer_id=lease.consumer_id,
            job_id=job_id,
            lease_id=lease.lease_id,
            generation=str(lease.generation),
            owned_root=lease.owned_root,
        )

    def recover(
        self, device: DeviceIdentity, job_id: str, lease: OwnershipLease | None = None
    ) -> OwnershipLease:
        """Restore an existing job's logical owner, never issue an ordinary claim."""
        if lease is None:
            result = self._broker("job_lease", device, consumer_id="plankton-usb", job_id=job_id)
            lease = self._lease_from_result(device, result, self._owned_root_parent)
        self._check_lease(device, lease)
        result = self._broker(
            "recover",
            device,
            consumer_id=lease.consumer_id,
            job_id=job_id,
            lease_id=lease.lease_id,
            generation=str(lease.generation),
            owned_root=lease.owned_root,
        )
        recovered = self._lease_from_result(device, result, self._owned_root_parent)
        if recovered != lease:
            raise OwnershipError("recovery changed logical ownership")
        if not self._verify_view(device, recovered):
            raise OwnershipUnavailable("USB ownership recovery is pending")
        self._leases[device.device_id] = recovered
        return recovered

    def reconcile(self, device: DeviceIdentity) -> bool:
        """Return any durable broker lease to the native desktop view."""
        result = self._broker("reconcile", device, consumer_id="plankton-usb")
        released = bool(result.get("released", False))
        if released:
            self._leases.pop(device.device_id, None)
        return released

    def eject(self, device: DeviceIdentity, lease: OwnershipLease | None = None) -> bool:
        lease = lease or self._leases.get(device.device_id)
        values: dict[str, str | Path] = {"consumer_id": "plankton-usb"}
        if lease is not None:
            self._check_lease(device, lease)
            values.update(
                owned_root=lease.owned_root,
                lease_id=lease.lease_id,
                generation=str(lease.generation),
            )
        result = self._broker("eject", device, **values)
        released = bool(result.get("released", lease is not None))
        if released:
            self._leases.pop(device.device_id, None)
        return released

    @staticmethod
    def _check_lease(device: DeviceIdentity, lease: OwnershipLease) -> None:
        if (
            lease.device_id != device.device_id
            or lease.filesystem_uuid != device.uuid
            or lease.hardware_serial != device.serial
            or lease.consumer_id != "plankton-usb"
        ):
            raise OwnershipError("stale ownership lease")

    @staticmethod
    def _lease_from_result(
        device: DeviceIdentity,
        result: dict[str, object],
        owned_root_parent: Path | None = Path("/media/plankton-usb"),
        native_user: str | None = None,
    ) -> OwnershipLease:
        owned_value = result.get("owned_root")
        if not isinstance(owned_value, str):
            raise OwnershipError("mount broker returned invalid ownership path")
        owned = Path(owned_value)
        if not owned.is_absolute() or (
            owned_root_parent is not None and owned.parent != owned_root_parent
        ):
            raise OwnershipError("mount broker returned invalid ownership path")
        lease_id = result.get("lease_id")
        generation_value = result.get("generation")
        consumer_id = result.get("consumer_id")
        if not isinstance(lease_id, str) or not lease_id or consumer_id != "plankton-usb":
            raise OwnershipError("mount broker returned invalid ownership lease")
        try:
            generation = int(str(generation_value))
        except (TypeError, ValueError) as error:
            raise OwnershipError("mount broker returned invalid ownership lease") from error
        if generation <= 0:
            raise OwnershipError("mount broker returned invalid ownership lease")
        result_user = result.get("native_user")
        if native_user is None and isinstance(result_user, str) and result_user:
            native_user = result_user
        return OwnershipLease(
            device_id=device.device_id,
            filesystem_uuid=device.uuid,
            hardware_serial=device.serial,
            consumer_id=consumer_id,
            owned_root=owned,
            lease_id=lease_id,
            generation=generation,
            native_user=native_user,
        )
