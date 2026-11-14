"""Kernel-observation provenance and bounded stale-mount cleanup regressions."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from host import usb_mount_broker as broker

pytest_plugins = ("tests.unit.test_issue296_broker_recovery",)
observe_mount = broker._mount_proof


@pytest.fixture
def stale(recovery: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    recovery.stale_present = True
    recovery.cleanup_count = 0
    recovery.cleanup_busy = False
    recovery.proof = json.loads(recovery.lease["mount_proof"])
    original = broker._mount_proof

    def observe(path: Path, fd: int) -> dict[str, str] | None:
        if recovery.stale_present:
            return dict(recovery.proof)
        return original(path, fd)

    def unmount(path: Path, *, lazy: bool = False, namespace_fd: int | None = None) -> None:
        assert path == recovery.owned and namespace_fd == 42 and not lazy
        recovery.cleanup_count += 1
        if recovery.cleanup_busy:
            raise broker.BrokerError("busy")
        recovery.stale_present = False

    original_mount = broker._mount_owned

    def mount(block: str, path: Path, **kwargs: Any) -> None:
        assert not recovery.stale_present, "never overmount"
        original_mount(block, path, **kwargs)

    monkeypatch.setattr(broker, "_mount_proof", observe)
    monkeypatch.setattr(broker, "_unmount", unmount)
    monkeypatch.setattr(broker, "_mount_owned", mount)
    return recovery


@pytest.mark.parametrize("field", ["id", "maj:min", "namespace", "boot", "fsroot", "fstype"])
def test_reused_path_with_different_mount_evidence_is_never_removed(stale: Any, field: str) -> None:
    stale.proof[field] = "different"
    before = broker.LEASES_PATH.read_bytes()
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(stale.request, namespace_fd=42)
    assert broker.LEASES_PATH.read_bytes() == before
    assert stale.cleanup_count == stale.mounts == stale.unmounts == 0


def test_legacy_stale_mount_without_proof_remains_pending(stale: Any) -> None:
    leases = broker._load_leases()
    next(iter(leases.values())).pop("mount_proof")
    broker._save_leases(leases)
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(stale.request, namespace_fd=42)
    assert broker._load_leases() == leases
    assert stale.cleanup_count == stale.mounts == stale.unmounts == 0


def test_busy_cleanup_restart_and_lost_reply_keep_one_owner(stale: Any) -> None:
    stale.cleanup_busy = True
    before = broker._load_leases()
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(stale.request, namespace_fd=42)
    assert broker._load_leases() == before and stale.stale_present
    assert stale.mounts == stale.unmounts == 0
    broker._reconcile_guard_markers()
    stale.cleanup_busy = False
    result = broker.handle(stale.request, namespace_fd=42)
    assert broker.handle(stale.request, namespace_fd=42) == result
    assert stale.cleanup_count == 2 and stale.mounts == 1
    lease = next(iter(broker._load_leases().values()))
    assert lease["job_id"] == stale.job and lease["lease_id"] == "original"
    assert lease["mount_diskseq"] == "11"


def test_other_lease_referring_to_same_mount_prevents_cleanup(stale: Any) -> None:
    leases = broker._load_leases()
    leases["other"] = {**stale.lease, "lease_id": "other", "uuid": "other", "serial": "other"}
    broker._save_leases(leases)
    with pytest.raises(broker.BrokerError, match="foreign or duplicate"):
        broker.handle(stale.request, namespace_fd=42)
    assert broker._load_leases() == leases
    assert stale.cleanup_count == stale.mounts == 0


def test_second_disconnect_retires_recorded_replacement_mount(stale: Any) -> None:
    first = broker.handle(stale.request, namespace_fd=42)
    lease = next(iter(broker._load_leases().values()))
    stale.proof = json.loads(lease["mount_proof"])
    stale.stale_present = True
    stale.consumer = False
    stale.native = True
    stale.seq = "12"
    stale.request.update(block_device="/dev/again1", drive_device="/dev/again")
    assert broker.handle(stale.request, namespace_fd=42) == first
    assert stale.cleanup_count == stale.mounts == 2
    assert {p.read_text() for p in broker.MOUNT_GUARD_DIR.iterdir()} == {"/dev/again1"}


def test_changing_observation_cannot_authorize_unmount(
    stale: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def changed(_path: Path, _fd: int) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {**stale.proof, "id": stale.proof["id"] if calls == 1 else "different"}

    monkeypatch.setattr(broker, "_mount_proof", changed)
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(stale.request, namespace_fd=42)
    assert stale.cleanup_count == stale.mounts == 0


def test_failed_unmount_postcondition_does_not_overmount(
    stale: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker, "_unmount", lambda *_args, **_kwargs: None)
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(stale.request, namespace_fd=42)
    assert stale.stale_present and stale.mounts == stale.unmounts == 0


def test_mount_proof_save_failure_preserves_waiting_responsibility(
    recovery: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    save = broker._save_leases

    def fail_new_proof(leases: dict[str, dict[str, str]]) -> None:
        if any(lease.get("mount_diskseq") == "11" for lease in leases.values()):
            raise OSError("proof persistence failed")
        save(leases)

    monkeypatch.setattr(broker, "_save_leases", fail_new_proof)
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(recovery.request, namespace_fd=42)
    lease = next(iter(broker._load_leases().values()))
    assert lease["recovering"] == "1" and lease["mount_diskseq"] == "10"
    # An unrecorded surviving mount cannot be adopted from its pathname on retry.
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(recovery.request, namespace_fd=42)
    assert recovery.mounts == 1


@pytest.mark.parametrize("missing", [False, True])
def test_kernel_mount_parser_requires_instance_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool,
) -> None:
    row = {"source": "/dev/original1", "target": str(tmp_path), "id": 123,
           "maj:min": "8:1", "fsroot": "/", "fstype": "exfat"}
    if missing:
        row.pop("id")

    def command(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "ID,MAJ:MIN" in argv[-1] and kwargs["namespace_fd"] >= 0
        return subprocess.CompletedProcess(argv, 0, json.dumps({"filesystems": [row]}), "")

    monkeypatch.setattr(broker, "_command", command)
    fd = os.open("/proc/self/ns/mnt", os.O_RDONLY)
    try:
        if missing:
            with pytest.raises(broker.RecoveryPending):
                observe_mount(tmp_path, fd)
        else:
            proof = observe_mount(tmp_path, fd)
            assert proof is not None and proof["id"] == "123" and proof["boot"]
            assert proof["namespace"] == f"{os.fstat(fd).st_dev}:{os.fstat(fd).st_ino}"
    finally:
        os.close(fd)
