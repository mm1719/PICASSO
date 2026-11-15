"""Real broker ledger/transactions; fake only privileged host observations/actions."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from host import usb_mount_broker as broker


@pytest.fixture
def claim_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    owned = tmp_path / "owned" / "uuid"
    owned.parent.mkdir()
    native_path = Path("/media/operator/LABEL")
    request = {
        "operation": "claim", "block_device": "/dev/fake1", "drive_device": "/dev/fake",
        "uuid": "uuid", "serial": "serial", "consumer_id": "plankton-usb",
    }
    state = SimpleNamespace(
        owned=owned, native_path=native_path, request=request, events=[],
        native={native_path: frozenset({"rw", "nodev", "nosuid"})}, consumer={},
        unmount_error=None, mount_error=None, release_error=None, restore_error=None,
        restore_noop=False, detached_ledger=None,
    )
    monkeypatch.setattr(broker, "LEASES_PATH", tmp_path / "leases.json")
    monkeypatch.setattr(broker, "MOUNT_GUARD_DIR", tmp_path / "guards")
    monkeypatch.setattr(broker, "SERVICE_ROOT", owned.parent)
    monkeypatch.setattr(broker, "_diskseq", lambda _drive: "123")
    monkeypatch.setattr("host.usb_mount_broker.os.chown", lambda *_args: None)
    monkeypatch.setattr("host.usb_mount_broker.pwd.getpwnam", lambda _user: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr("host.usb_mount_broker.grp.getgrnam", lambda _group: SimpleNamespace(gr_gid=1234))
    monkeypatch.setattr(broker, "_command", lambda *_args, **_kw: pytest.fail("no host commands"))

    def identity(values: dict[str, str]) -> None:
        if any(values[key] != request[key] for key in ("uuid", "serial", "block_device", "drive_device")):
            raise broker.BrokerError("USB identity changed")

    def mounts(_block: str, namespace_fd: int | None = None) -> dict[Path, frozenset[str]]:
        return dict(state.native if namespace_fd is None else state.consumer)

    def unmount(_block: str) -> None:
        state.detached_ledger = broker._lease_for(request)
        state.events.append("detach-native")
        state.native.clear()
        if state.unmount_error:
            raise state.unmount_error

    def mount(_block: str, path: Path, *, namespace_fd: int | None) -> None:
        assert namespace_fd == 4242
        assert not state.native, "never create a second writer"
        state.events.append("mount-owned")
        state.consumer[path] = frozenset({"rw", "nodev", "nosuid", "noexec"})
        if state.mount_error:
            raise state.mount_error

    def release(path: Path, *, namespace_fd: int | None, lazy: bool = False) -> None:
        assert namespace_fd == 4242 and not lazy
        state.events.append("release-owned")
        if state.release_error:
            raise state.release_error
        state.consumer.pop(path)

    def restore(_block: str, user: str) -> None:
        assert user == "operator"
        assert not state.consumer, "never restore a second writer"
        state.events.append("restore-native")
        if state.restore_error:
            raise state.restore_error
        if not state.restore_noop:
            state.native[native_path] = frozenset({"rw", "nodev", "nosuid"})

    monkeypatch.setattr(broker, "_identity", identity)
    monkeypatch.setattr(broker, "_mounts", mounts)
    monkeypatch.setattr(broker, "_unmount_device", unmount)
    monkeypatch.setattr(broker, "_mount_owned", mount)
    monkeypatch.setattr(broker, "_unmount", release)
    monkeypatch.setattr(broker, "_restore_native", restore)
    return state


def test_claim_intent_is_durable_before_unmount_and_success_commits_same_identity(claim_host: Any) -> None:
    result = broker.handle(claim_host.request, namespace_fd=4242)
    intent = claim_host.detached_ledger
    assert intent is not None
    assert intent["cleanup_pending"] == "1"
    assert intent["cleanup_target"] == "native_rw"
    assert intent["transition_stage"] == "detaching_native"
    assert intent["native_user"] == "operator"
    assert intent["diskseq"] == "123"
    assert "issued_at" not in intent
    assert result["ok"] is True
    lease = broker._lease_for(claim_host.request)
    assert lease is not None and "cleanup_pending" not in lease and lease["issued_at"]
    assert lease["lease_id"] == intent["lease_id"] == result["lease_id"]
    assert lease["generation"] == intent["generation"] == result["generation"]
    assert broker._guard_marker(claim_host.request["block_device"]).exists()


@pytest.mark.parametrize("point", ["detach", "partial-mount"])
def test_failed_claim_rolls_back_in_order_then_clears_only_its_responsibility(
    claim_host: Any, point: str,
) -> None:
    primary = broker.BrokerError("original claim failure")
    setattr(claim_host, "unmount_error" if point == "detach" else "mount_error", primary)
    other = {"block_device": "/dev/other1", "uuid": "other", "serial": "other", "lease_id": "other"}
    broker._save_leases({broker._lease_key(other): other})
    broker._guard_acquire(other["block_device"])

    with pytest.raises(broker.BrokerError) as raised:
        broker.handle(claim_host.request, namespace_fd=4242)

    assert raised.value is primary
    assert claim_host.events == (
        ["detach-native"] + (["mount-owned", "release-owned"] if point == "partial-mount" else [])
        + ["restore-native"]
    )
    assert not claim_host.consumer and claim_host.native
    assert broker._load_leases() == {broker._lease_key(other): other}
    assert broker._guard_marker(other["block_device"]).exists()
    assert not broker._guard_marker(claim_host.request["block_device"]).exists()


@pytest.mark.parametrize("point", ["restore", "controlled-unmount", "restore-noop"])
def test_pending_claim_survives_restart_reconciles_and_preserves_both_errors(
    claim_host: Any, caplog: pytest.LogCaptureFixture, point: str,
) -> None:
    claim_host.mount_error = broker.BrokerError("primary mount failed after partial success")
    if point == "controlled-unmount":
        claim_host.release_error = broker.BrokerError("controlled unmount failed")
    elif point == "restore":
        claim_host.restore_error = broker.BrokerError("native rollback failed")
    else:
        claim_host.restore_noop = True
    with pytest.raises(broker.BrokerError, match="USB ownership cleanup is pending"):
        broker.handle(claim_host.request, namespace_fd=4242)
    lease = broker._lease_for(claim_host.request)
    assert lease is not None
    assert lease["cleanup_pending"] == "1"
    assert lease["primary_error"] == "BrokerError: primary mount failed after partial success"
    assert lease["rollback_error"].startswith("BrokerError:")
    assert "claim failed lease=" in caplog.text and "claim rollback failed lease=" in caplog.text
    assert "primary mount failed after partial success" in caplog.text
    assert lease["rollback_error"].split(": ", 1)[1] in caplog.text
    if point == "controlled-unmount":
        assert "restore-native" not in claim_host.events

    # Reconstruct startup state from disk, not from a returned/in-memory lease.
    broker._guard_marker(claim_host.request["block_device"]).unlink()
    broker._reconcile_guard_markers()
    assert broker._guard_marker(claim_host.request["block_device"]).exists()
    assert broker._lease_for(claim_host.request) == lease
    with pytest.raises(broker.BrokerError, match="cleanup is pending"):
        broker.handle(claim_host.request, namespace_fd=4242)
    before = list(claim_host.events)
    with pytest.raises(broker.BrokerError, match="identity changed"):
        broker.handle({**claim_host.request, "operation": "reconcile", "serial": "wrong"}, namespace_fd=4242)
    assert claim_host.events == before and broker._lease_for(claim_host.request) == lease
    claim_host.release_error = claim_host.restore_error = None
    claim_host.restore_noop = False
    assert broker.handle({**claim_host.request, "operation": "reconcile"}, namespace_fd=4242) == {
        "ok": True, "released": True,
    }
    assert not claim_host.consumer and claim_host.native
    assert broker._load_leases() == {}
    assert not broker._guard_marker(claim_host.request["block_device"]).exists()


def test_intent_storage_failure_prevents_any_mount_transition(
    claim_host: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_save(_leases: dict[str, dict[str, str]]) -> None:
        raise OSError("disk unavailable")
    monkeypatch.setattr(broker, "_save_leases", fail_save)
    with pytest.raises(OSError, match="disk unavailable"):
        broker.handle(claim_host.request, namespace_fd=4242)
    assert claim_host.events == [] and claim_host.native and not claim_host.consumer


def test_pending_record_cannot_be_used_as_accepted_job_lease(claim_host: Any) -> None:
    claim_host.unmount_error = broker.BrokerError("detach failed")
    claim_host.restore_error = broker.BrokerError("restore failed")
    with pytest.raises(broker.BrokerError, match="cleanup is pending"):
        broker.handle(claim_host.request, namespace_fd=4242)
    lease = broker._lease_for(claim_host.request)
    assert lease is not None
    for operation in ("job_lease", "bind_job", "recover"):
        with pytest.raises(broker.BrokerError, match="cleanup is pending"):
            broker.handle({
                **claim_host.request, "operation": operation,
                "job_id": "11111111-2222-4333-8444-555555555555",
                "lease_id": lease["lease_id"], "generation": lease["generation"],
            }, namespace_fd=4242)
    assert broker._lease_for(claim_host.request) == lease


@pytest.mark.parametrize("contents", ["{", "[]", '{"lease":null}', '{"lease":{"generation":1}}'])
def test_invalid_ledger_cannot_be_treated_as_empty_or_retire_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str,
) -> None:
    monkeypatch.setattr(broker, "LEASES_PATH", tmp_path / "leases.json")
    monkeypatch.setattr(broker, "MOUNT_GUARD_DIR", tmp_path / "guards")
    broker.LEASES_PATH.write_text(contents)
    broker._guard_acquire("/dev/fake1")
    with pytest.raises(broker.BrokerError, match="ledger"):
        broker._reconcile_guard_markers()
    assert broker._guard_marker("/dev/fake1").exists()
    assert broker.LEASES_PATH.read_text() == contents


def test_reconcile_failure_retains_pending_record_for_next_retry(claim_host: Any) -> None:
    claim_host.unmount_error = broker.BrokerError("detach failed")
    claim_host.restore_error = broker.BrokerError("restore failed")
    with pytest.raises(broker.BrokerError):
        broker.handle(claim_host.request, namespace_fd=4242)
    before = json.loads(broker.LEASES_PATH.read_text())
    with pytest.raises(broker.BrokerError, match="restore failed"):
        broker.handle({**claim_host.request, "operation": "reconcile"}, namespace_fd=4242)
    assert json.loads(broker.LEASES_PATH.read_text()) == before
    assert broker._guard_marker(claim_host.request["block_device"]).exists()


def test_process_death_after_detach_leaves_reconcilable_intent(claim_host: Any) -> None:
    # The child dies without executing exception handlers or returning a lease.
    script = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        from host import usb_mount_broker as broker
        broker.LEASES_PATH = Path(sys.argv[1])
        broker.MOUNT_GUARD_DIR = Path(sys.argv[2])
        broker.SERVICE_ROOT = Path(sys.argv[3])
        broker._identity = lambda request: None
        broker._diskseq = lambda drive: '123'
        broker._mounts = lambda block, namespace_fd=None: {
            Path('/media/operator/LABEL'): frozenset({'rw', 'nodev', 'nosuid'})
        }
        broker._unmount_device = lambda block: os._exit(73)
        broker.handle(json.loads(sys.argv[4]), namespace_fd=4242)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(broker.LEASES_PATH), str(broker.MOUNT_GUARD_DIR),
         str(broker.SERVICE_ROOT), json.dumps(claim_host.request)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 73, result.stderr
    lease = broker._lease_for(claim_host.request)
    assert lease is not None and lease["transition_stage"] == "detaching_native"
    assert lease["cleanup_pending"] == "1" and "issued_at" not in lease
    claim_host.native.clear()
    broker._reconcile_guard_markers()
    assert broker.handle({**claim_host.request, "operation": "reconcile"}, namespace_fd=4242) == {
        "ok": True, "released": True,
    }
    assert claim_host.events == ["restore-native"] and claim_host.native
    assert broker._load_leases() == {}


@pytest.mark.parametrize("fault", ["duplicate-native", "controlled-returned"])
def test_rollback_does_not_finalize_with_duplicate_or_returned_view(
    claim_host: Any, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    claim_host.unmount_error = broker.BrokerError("detach failed")

    def unsafe_restore(_block: str, _user: str) -> None:
        claim_host.native[claim_host.native_path] = frozenset({"rw", "nodev", "nosuid"})
        if fault == "duplicate-native":
            claim_host.native[Path("/media/operator/OTHER")] = frozenset({"rw", "nodev", "nosuid"})
        else:
            claim_host.consumer[claim_host.owned] = frozenset({"rw", "nodev", "nosuid", "noexec"})

    monkeypatch.setattr(broker, "_restore_native", unsafe_restore)
    with pytest.raises(broker.BrokerError, match="cleanup is pending"):
        broker.handle(claim_host.request, namespace_fd=4242)
    lease = broker._lease_for(claim_host.request)
    assert lease is not None and "postcondition failed" in lease["rollback_error"]


@pytest.mark.parametrize("fault", ["guard", "commit", "diagnostics", "finalize"])
def test_storage_or_guard_failure_never_drops_unfinished_cleanup(
    claim_host: Any, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    save = broker._save_leases

    def fail_save(leases: dict[str, dict[str, str]]) -> None:
        record = leases.get(broker._lease_key(claim_host.request), {})
        if ((fault == "commit" and record.get("issued_at"))
            or (fault == "diagnostics" and record.get("primary_error"))
            or (fault == "finalize" and not leases)):
            raise OSError("ledger write failed")
        save(leases)

    monkeypatch.setattr(broker, "_save_leases", fail_save)
    if fault == "guard":
        def fail_guard(_block: str) -> None:
            raise OSError("guard creation failed")
        monkeypatch.setattr(broker, "_guard_acquire", fail_guard)
        with pytest.raises(OSError, match="guard creation failed"):
            broker.handle(claim_host.request, namespace_fd=4242)
        assert claim_host.events == ["restore-native"] and claim_host.native
        assert broker._load_leases() == {}
        return
    if fault in {"diagnostics", "finalize"}:
        claim_host.unmount_error = broker.BrokerError("detach failed")
    if fault != "finalize":
        claim_host.restore_error = broker.BrokerError("restore failed")
    with pytest.raises(broker.BrokerError, match="cleanup is pending"):
        broker.handle(claim_host.request, namespace_fd=4242)
    lease = broker._lease_for(claim_host.request)
    assert lease is not None and lease["cleanup_pending"] == "1"
    assert "issued_at" not in lease
    monkeypatch.setattr(broker, "_save_leases", save)
    claim_host.restore_error = None
    assert broker.handle({**claim_host.request, "operation": "reconcile"}, namespace_fd=4242)["released"]
    assert broker._load_leases() == {} and claim_host.native and not claim_host.consumer


def test_concurrent_claims_cannot_replace_the_first_durable_owner(claim_host: Any) -> None:
    def claim() -> dict[str, Any]:
        try:
            return broker.handle(claim_host.request, namespace_fd=4242)
        except broker.BrokerError as error:
            return {"error": str(error)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    success = [result for result in results if result.get("ok") is True]
    assert len(success) == 1
    assert {"error": "device is already owned"} in results
    assert claim_host.events == ["detach-native", "mount-owned"]
    lease = broker._lease_for(claim_host.request)
    assert lease is not None and lease["lease_id"] == success[0]["lease_id"]


def test_unreadable_ledger_fails_closed_without_removing_guard(
    claim_host: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker._guard_acquire(claim_host.request["block_device"])

    def unreadable(_path: Path, **_kwargs: object) -> str:
        raise PermissionError("ledger unavailable")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(broker.BrokerError, match="ledger is unavailable"):
        broker._reconcile_guard_markers()
    assert broker._guard_marker(claim_host.request["block_device"]).exists()
    assert claim_host.events == []


@pytest.mark.parametrize("point", ["observe-owned", "unmount-owned", "restore-native"])
def test_release_revalidates_identity_after_each_slow_boundary(
    claim_host: Any, monkeypatch: pytest.MonkeyPatch, point: str,
) -> None:
    state = claim_host
    result = broker.handle(state.request, namespace_fd=4242)
    lease = broker._lease_for(state.request)
    assert lease is not None
    state.identity_valid = True
    state.events.clear()
    mounts, unmount, restore = broker._mounts, broker._unmount, broker._restore_native

    def identity(_request: dict[str, str]) -> None:
        if not state.identity_valid:
            raise broker.BrokerError("filesystem identity changed")

    def observe(block: str, namespace_fd: int | None = None) -> dict[Path, frozenset[str]]:
        observed = mounts(block, namespace_fd)
        if point == "observe-owned":
            state.identity_valid = False
        return observed

    def detach(path: Path, *, namespace_fd: int | None, lazy: bool = False) -> None:
        unmount(path, namespace_fd=namespace_fd, lazy=lazy)
        if point == "unmount-owned":
            state.identity_valid = False

    def native(block: str, user: str) -> None:
        restore(block, user)
        if point == "restore-native":
            state.identity_valid = False

    monkeypatch.setattr(broker, "_identity", identity)
    monkeypatch.setattr(broker, "_mounts", observe)
    monkeypatch.setattr(broker, "_unmount", detach)
    monkeypatch.setattr(broker, "_restore_native", native)
    with pytest.raises(broker.BrokerError, match="identity changed"):
        broker.handle({
            **state.request, "operation": "release", "lease_id": result["lease_id"],
            "generation": result["generation"], "owned_root": str(state.owned),
        }, namespace_fd=4242)
    assert broker._lease_for(state.request) == lease
    assert broker._guard_marker(state.request["block_device"]).exists()
    assert state.events == {
        "observe-owned": [], "unmount-owned": ["release-owned"],
        "restore-native": ["release-owned", "restore-native"],
    }[point]
