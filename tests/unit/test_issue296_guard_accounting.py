"""Guard retirement follows the same durable owner through retry and terminal cleanup."""

import json
import subprocess
from typing import Any

import pytest

from host import usb_mount_broker as broker

pytest_plugins = ("tests.unit.test_issue296_broker_recovery",)


def _pending_returns(recovery: Any) -> None:
    recovery.busy = True
    for sequence, path in (("11", "/dev/returned"), ("12", "/dev/again")):
        recovery.seq = sequence
        recovery.request.update(block_device=path + "1", drive_device=path)
        with pytest.raises(broker.BrokerError, match="recovery is pending"):
            broker.handle(recovery.request, namespace_fd=42)


def _guards() -> set[str]:
    return {path.read_text() for path in broker.MOUNT_GUARD_DIR.iterdir()}


def test_restart_retains_every_pending_guard_then_third_return_cleans_it(recovery: Any) -> None:
    _pending_returns(recovery)
    expected = {"/dev/original1", "/dev/returned1", "/dev/again1"}
    lease = next(iter(broker._load_leases().values()))
    assert set(json.loads(lease["guard_blocks"])) == expected
    for path in broker.MOUNT_GUARD_DIR.iterdir():
        path.unlink()
    broker._reconcile_guard_markers()
    assert _guards() == expected
    recovery.busy = False
    recovery.seq = "13"
    recovery.request.update(block_device="/dev/third1", drive_device="/dev/third")
    assert broker.handle(recovery.request, namespace_fd=42)["ok"]
    assert _guards() == {"/dev/third1"}
    assert "guard_blocks" not in next(iter(broker._load_leases().values()))


def test_retirement_preserves_another_owners_pending_reference(recovery: Any) -> None:
    _pending_returns(recovery)
    other = {**recovery.lease, "block_device": "/dev/unrelated1", "uuid": "unrelated",
             "serial": "unrelated", "lease_id": "unrelated", "recovery_block": "/dev/returned1"}
    leases = broker._load_leases()
    leases[broker._lease_key(other)] = other
    broker._save_leases(leases)
    broker._guard_acquire("/dev/unrelated1")
    recovery.busy = False
    assert broker.handle(recovery.request, namespace_fd=42)["ok"]
    assert _guards() == {"/dev/again1", "/dev/unrelated1", "/dev/returned1"}
    assert broker._load_leases()[broker._lease_key(other)] == other


def test_failed_guard_cleanup_keeps_durable_retry_responsibility(
    recovery: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pending_returns(recovery)
    recovery.busy = False
    original = broker._guard_release

    def fail_once(block: str) -> None:
        if block == "/dev/returned1":
            raise PermissionError("injected guard unlink failure")
        original(block)

    with monkeypatch.context() as context:
        context.setattr(broker, "_guard_release", fail_once)
        with pytest.raises(broker.BrokerError, match="recovery is pending"):
            broker.handle(recovery.request, namespace_fd=42)
    lease = next(iter(broker._load_leases().values()))
    assert "/dev/returned1" in json.loads(lease["guard_blocks"])
    assert lease["recovering"] == "1"
    broker._reconcile_guard_markers()
    assert broker.handle(recovery.request, namespace_fd=42)["ok"]
    assert recovery.mounts == 1
    assert _guards() == {"/dev/again1"}


@pytest.mark.parametrize("operation", ["release", "eject"])
def test_terminal_cleanup_retires_all_tracked_guards(
    recovery: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _pending_returns(recovery)
    monkeypatch.setattr(broker, "_mounts", lambda *_args: {})
    monkeypatch.setattr(broker, "_restore_native", lambda *_args: None)
    monkeypatch.setattr(broker, "_command", lambda argv, **_: subprocess.CompletedProcess(argv, 0, "", ""))
    assert broker.handle({**recovery.request, "operation": operation}, namespace_fd=42)["released"]
    assert broker._load_leases() == {}
    assert _guards() == set()


@pytest.mark.parametrize("operation", ["release", "eject"])
def test_terminal_cleanup_does_not_finalize_when_guard_retirement_fails(
    recovery: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _pending_returns(recovery)
    monkeypatch.setattr(broker, "_mounts", lambda *_args: {})
    monkeypatch.setattr(broker, "_restore_native", lambda *_args: None)
    monkeypatch.setattr(broker, "_command", lambda argv, **_: subprocess.CompletedProcess(argv, 0, "", ""))
    before = broker._load_leases()

    def fail(_block: str) -> None:
        raise PermissionError("injected guard unlink failure")

    monkeypatch.setattr(broker, "_guard_release", fail)
    with pytest.raises(broker.RecoveryPending):
        broker.handle({**recovery.request, "operation": operation}, namespace_fd=42)
    assert broker._load_leases() == before


def test_pending_save_failure_cannot_create_an_unrecorded_guard(
    recovery: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = broker._load_leases()

    def fail(_leases: object) -> None:
        raise OSError("injected pending save failure")

    monkeypatch.setattr(broker, "_save_leases", fail)
    with pytest.raises(OSError, match="pending save"):
        broker.handle(recovery.request, namespace_fd=42)
    assert broker._load_leases() == before
    assert _guards() == {"/dev/original1"}
    assert recovery.mounts == recovery.unmounts == 0


def test_final_save_failure_retains_guard_history_for_retry(
    recovery: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pending_returns(recovery)
    recovery.busy = False
    original = broker._save_leases

    def fail_final(leases: dict[str, dict[str, str]]) -> None:
        if not any(lease.get("recovering") for lease in leases.values()):
            raise OSError("injected final save failure")
        original(leases)

    with monkeypatch.context() as context:
        context.setattr(broker, "_save_leases", fail_final)
        with pytest.raises(OSError, match="final save"):
            broker.handle(recovery.request, namespace_fd=42)
    assert next(iter(broker._load_leases().values()))["guard_blocks"]
    broker._reconcile_guard_markers()
    assert broker.handle(recovery.request, namespace_fd=42)["ok"]
    assert recovery.mounts == 1
    assert _guards() == {"/dev/again1"}
