"""Recovery must request the parent relation needed for partition serial identity."""

import json
import subprocess
from typing import Any

import pytest

from host import usb_mount_broker as broker

pytest_plugins = ("tests.unit.test_issue296_broker_recovery",)


@pytest.mark.parametrize("case", ["changed", "reused", "absent", "serial", "duplicate", "path"])
def test_recovery_observes_partition_parent(
    recovery: Any, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    if case == "reused":
        recovery.request.update(block_device="/dev/original1", drive_device="/dev/original")
    original_command = broker._command

    def command(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] != "/usr/bin/lsblk":
            return original_command(argv, **kwargs)
        partition = {
            "path": "/dev/other1" if case == "path" else recovery.request["block_device"],
            "uuid": "uuid",
            "serial": None,
        }
        disk = {
            "path": recovery.request["drive_device"],
            "uuid": None,
            "serial": "wrong" if case == "serial" else "serial",
        }
        # PATH alone does not enable lsblk's children relationship. Only an
        # explicit tree request supplies the parent serial to this partition.
        nodes = (
            [{**disk, "children": [partition]}]
            if "--tree" in argv
            else [disk, partition]
        )
        if case == "absent":
            nodes = []
        elif case == "duplicate":
            nodes = nodes * 2
        return subprocess.CompletedProcess(argv, 0, json.dumps({"blockdevices": nodes}), "")

    monkeypatch.setattr(broker, "_command", command)
    before = broker.LEASES_PATH.read_bytes()
    guards = {path.name: path.read_bytes() for path in broker.MOUNT_GUARD_DIR.iterdir()}
    if case not in {"changed", "reused"}:
        with pytest.raises(broker.BrokerError, match="identity is absent or ambiguous"):
            broker.handle(recovery.request, namespace_fd=42)
        assert recovery.mounts == recovery.unmounts == 0
        assert broker.LEASES_PATH.read_bytes() == before
        assert {p.name: p.read_bytes() for p in broker.MOUNT_GUARD_DIR.iterdir()} == guards
        return

    response = broker.handle(recovery.request, namespace_fd=42)
    assert response["ok"]
    assert response["lease_id"] == "original" and response["generation"] == "1"
    leases = broker._load_leases()
    assert len(leases) == 1
    lease = next(iter(leases.values()))
    assert lease["job_id"] == recovery.job
    assert lease["block_device"] == recovery.request["block_device"]
    assert broker.handle(recovery.request, namespace_fd=42) == response
    assert recovery.mounts == recovery.unmounts == 1
    assert {p.read_text() for p in broker.MOUNT_GUARD_DIR.iterdir()} == {
        recovery.request["block_device"],
    }
