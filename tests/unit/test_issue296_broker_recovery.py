"""Real broker transactions and SQLite authority; only OS observations/actions are fake."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from plankton_core import JobStore

from host import usb_mount_broker as broker


@pytest.fixture
def recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    database = tmp_path / "app.db"
    store = JobStore(database)
    store.initialize()
    job = str(uuid4())
    store.create_requested_job(
        job, "model", "a" * 64, "device", "science", device_uuid="uuid", device_serial="serial"
    )
    owned = tmp_path / "owned" / "uuid"
    owned.parent.mkdir()
    request = {
        "operation": "recover",
        "block_device": "/dev/returned1",
        "drive_device": "/dev/returned",
        "uuid": "uuid",
        "serial": "serial",
        "consumer_id": "plankton-usb",
        "job_id": job,
        "lease_id": "original",
        "generation": "1",
        "owned_root": str(owned),
    }
    lease = dict(
        request,
        block_device="/dev/original1",
        drive_device="/dev/original",
        diskseq="10",
        issued_at="2000-01-01T00:00:00+00:00",
        native_user="operator",
    )
    lease.pop("operation")
    monkeypatch.setattr(broker, "LEASES_PATH", tmp_path / "leases.json")
    monkeypatch.setattr(broker, "MOUNT_GUARD_DIR", tmp_path / "guards")
    monkeypatch.setattr(broker, "SERVICE_ROOT", owned.parent)
    monkeypatch.setattr(broker, "_job_database", lambda _fd: database)
    state = SimpleNamespace(
        native=True,
        consumer=False,
        seq="11",
        mounts=0,
        unmounts=0,
        unsafe=False,
        duplicate=False,
        foreign=False,
        busy=False,
        partial=False,
        after_mount=None,
        request=request,
        lease=lease,
        database=database,
        owned=owned,
        store=store,
        job=job,
    )
    broker._save_leases({broker._lease_key(lease): lease})
    broker._guard_acquire(lease["block_device"])
    monkeypatch.setattr(broker, "_diskseq", lambda _drive: state.seq)
    monkeypatch.setattr(broker, "_identity", lambda _request: None)
    monkeypatch.setattr("host.usb_mount_broker.os.chown", lambda *_args: None)
    monkeypatch.setattr("host.usb_mount_broker.pwd.getpwnam", lambda _name: SimpleNamespace(pw_uid=10002))
    monkeypatch.setattr("host.usb_mount_broker.grp.getgrnam", lambda _name: SimpleNamespace(gr_gid=10002))

    def mounts(_block: str, fd: int | None = None) -> dict[Path, frozenset[str]]:
        if fd is None:
            return (
                {Path("/media/operator/usb"): frozenset({"rw", "nodev", "nosuid"})}
                if state.native
                else {}
            )
        return (
            {owned: frozenset({"ro" if state.unsafe else "rw", "nodev", "nosuid", "noexec"})}
            if state.consumer
            else {}
        )

    def command(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0].endswith("lsblk"):
            node = {"path": request["block_device"], "uuid": "uuid", "serial": "serial"}
            value = {"blockdevices": [node, node] if state.duplicate else [node]}
        elif argv[0].endswith("findmnt"):
            value = {
                "filesystems": [
                    {
                        "source": "/dev/foreign" if state.foreign else request["block_device"],
                        "target": str(owned),
                    }
                ]
                if state.consumer or state.foreign
                else []
            }
        elif "unmount" in argv:
            state.unmounts += 1
            if state.busy:
                raise broker.BrokerError("busy")
            state.native = False
            value = {}
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    def mount(_block: str, _owned: Path, **_kwargs: object) -> None:
        state.mounts += 1
        state.consumer = not state.partial
        if state.after_mount is not None:
            state.after_mount()

    monkeypatch.setattr(broker, "_mounts", mounts)
    monkeypatch.setattr(broker, "_command", command)
    monkeypatch.setattr(broker, "_mount_owned", mount)

    def mount_proof(path: Path, namespace_fd: int) -> dict[str, str] | None:
        # Simulated kernel mount instances are independent of the persisted lease.
        # Consult the dynamic observation so the tester stale-mount replay remains
        # effective without editing its assertions or its OS command wrapper.
        raw = json.loads(broker._command(
            ("/usr/bin/findmnt", "--json", "--list", "--output", "SOURCE,TARGET,OPTIONS"),
            namespace_fd=namespace_fd,
        ).stdout)
        targets = [m for m in raw["filesystems"] if m.get("target") == str(path)]
        if len(targets) > 1:
            raise broker.BrokerError("foreign or duplicate recovery view")
        if not targets:
            return None
        source = targets[0]["source"]
        seq = "10" if source == "/dev/original1" and not state.consumer else state.seq
        return {"source": source, "target": str(path), "id": seq,
                "maj:min": "8:1", "fsroot": "/", "fstype": "exfat",
                "namespace": "4:42", "boot": "test-boot"}

    monkeypatch.setattr(broker, "_mount_proof", mount_proof)
    lease.update(mount_proof=json.dumps({
        "source": "/dev/original1", "target": str(owned), "id": "10", "maj:min": "8:1",
        "fsroot": "/", "fstype": "exfat", "namespace": "4:42", "boot": "test-boot",
    }), mount_diskseq="10")
    broker._save_leases({broker._lease_key(lease): lease})
    return state


def test_recovery_preserves_one_lease_and_is_idempotent_after_lost_reply(recovery: Any) -> None:
    first = broker.handle(recovery.request, namespace_fd=42)
    assert first["lease_id"] == "original" and first["generation"] == "1"
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(lambda _: broker.handle(recovery.request, namespace_fd=42), range(2))
        )
    assert replies == [first, first]
    assert recovery.mounts == recovery.unmounts == 1
    leases = broker._load_leases()
    assert len(leases) == 1
    lease = next(iter(leases.values()))
    assert lease["job_id"] == recovery.job and lease["block_device"] == "/dev/returned1"
    assert not broker._guard_marker("/dev/original1").exists()
    assert broker._guard_marker("/dev/returned1").exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "terminal",
        "missing",
        "revoked",
        "stale",
        "consumer",
        "wrong_uuid",
        "wrong_serial",
        "duplicate",
        "no_disconnect",
        "foreign",
        "unsafe",
    ],
)
def test_recovery_rejects_invalid_authority_or_view(recovery: Any, invalid: str) -> None:
    if invalid in {"terminal", "missing"}:
        with sqlite3.connect(recovery.database) as connection:
            connection.execute("UPDATE jobs SET state = 'FAILED' WHERE job_id = ?", (recovery.job,))
        if invalid == "missing":
            recovery.request["job_id"] = str(uuid4())
    elif invalid == "revoked":
        broker._save_leases({})
    elif invalid == "stale":
        recovery.request["generation"] = "2"
    elif invalid == "consumer":
        recovery.request["consumer_id"] = "foreign"
    elif invalid.startswith("wrong_"):
        recovery.request[invalid.removeprefix("wrong_")] = "wrong"
    elif invalid == "duplicate":
        recovery.duplicate = True
    elif invalid == "no_disconnect":
        recovery.seq = "10"
    elif invalid == "foreign":
        recovery.foreign = True
    elif invalid == "unsafe":
        recovery.consumer = recovery.unsafe = True
    before = broker._load_leases()
    with pytest.raises(broker.BrokerError):
        broker.handle(recovery.request, namespace_fd=42)
    assert recovery.mounts == recovery.unmounts == 0
    assert broker._load_leases() == before


@pytest.mark.parametrize("fault", ["busy", "partial"])
def test_partial_recovery_retains_responsibility_across_restart(recovery: Any, fault: str) -> None:
    setattr(recovery, fault, True)
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(recovery.request, namespace_fd=42)
    lease = next(iter(broker._load_leases().values()))
    assert lease["recovering"] == "1" and lease["lease_id"] == "original"
    broker._reconcile_guard_markers()
    assert broker._guard_marker("/dev/returned1").exists()
    reconcile = {
        **recovery.request,
        "operation": "reconcile",
        "owned_root": "",
        "lease_id": "",
        "generation": "",
        "job_id": "",
    }
    assert broker.handle(reconcile, namespace_fd=42)["released"] is False
    lookup = {
        **recovery.request,
        "operation": "job_lease",
        "owned_root": "",
        "lease_id": "",
        "generation": "",
    }
    assert broker.handle(lookup, namespace_fd=42)["lease_id"] == "original"
    setattr(recovery, fault, False)
    assert broker.handle(recovery.request, namespace_fd=42)["ok"]


def test_revocation_before_commit_cannot_publish_success(recovery: Any) -> None:
    recovery.after_mount = lambda: broker._save_leases({})
    with pytest.raises(broker.BrokerError, match="revoked"):
        broker.handle(recovery.request, namespace_fd=42)
    assert broker._load_leases() == {}


def test_terminal_transition_is_fenced_during_recovery(recovery: Any) -> None:
    def terminal() -> None:
        with (
            sqlite3.connect(recovery.database, timeout=0) as connection,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            connection.execute("UPDATE jobs SET state = 'FAILED'")

    recovery.after_mount = terminal
    assert broker.handle(recovery.request, namespace_fd=42)["ok"]


def test_later_job_cannot_adopt_terminal_owners_lease(recovery: Any) -> None:
    with sqlite3.connect(recovery.database) as connection:
        connection.execute("UPDATE jobs SET state = 'FAILED'")
    later = str(uuid4())
    recovery.store.create_requested_job(
        later, "model", "a" * 64, "device", "science", device_uuid="uuid", device_serial="serial"
    )
    recovery.request["job_id"] = later
    with pytest.raises(broker.BrokerError, match="no longer authorized"):
        broker.handle(recovery.request, namespace_fd=42)
    assert recovery.mounts == 0


def test_only_verified_import_can_bind_original_job(recovery: Any) -> None:
    lease = {k: v for k, v in recovery.lease.items() if k != "job_id"}
    lease.update(block_device=recovery.request["block_device"], diskseq=recovery.seq)
    broker._save_leases({broker._lease_key(lease): lease})
    with pytest.raises(broker.BrokerError, match="no longer authorized"):
        broker.handle(recovery.request, namespace_fd=42)
    binding = {**recovery.request, "operation": "bind_job"}
    with pytest.raises(broker.BrokerError, match="no longer authorized"):
        broker.handle(binding, namespace_fd=42)
    with sqlite3.connect(recovery.database) as connection:
        connection.execute("UPDATE jobs SET state = 'SCANNING'")
    with pytest.raises(broker.BrokerError, match="current exclusive ownership"):
        broker.handle(binding, namespace_fd=42)
    recovery.native, recovery.consumer = False, True
    assert broker.handle(binding, namespace_fd=42)["lease_id"] == "original"
    bound = next(iter(broker._load_leases().values()))
    assert bound["job_id"] == recovery.job and bound["database"]
    assert recovery.mounts == recovery.unmounts == 0


def test_recovery_preserves_unrelated_lease_and_guard(recovery: Any) -> None:
    other = {**recovery.lease, "block_device": "/dev/other1", "uuid": "other",
             "serial": "other", "lease_id": "unrelated"}
    leases = broker._load_leases()
    leases[broker._lease_key(other)] = other
    broker._save_leases(leases)
    broker._guard_acquire("/dev/other1")
    broker.handle(recovery.request, namespace_fd=42)
    assert broker._load_leases()[broker._lease_key(other)] == other
    assert broker._guard_marker("/dev/other1").exists()


def test_commit_rejects_changed_generation(recovery: Any) -> None:
    def revoke() -> None:
        leases = broker._load_leases()
        next(iter(leases.values()))["generation"] = "2"
        broker._save_leases(leases)
    recovery.after_mount = revoke
    with pytest.raises(broker.BrokerError, match="stale ownership"):
        broker.handle(recovery.request, namespace_fd=42)
    assert next(iter(broker._load_leases().values()))["generation"] == "2"


def test_database_selection_uses_namespace_inode_not_caller_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, test = tmp_path / "prod.db", tmp_path / "test.db"
    production.touch()
    test.touch()
    monkeypatch.setattr(broker, "JOB_DATABASES", (production, test))
    stat = test.stat()
    monkeypatch.setattr(broker, "_command", lambda argv, **_: subprocess.CompletedProcess(
        argv, 0, f"{stat.st_dev}:{stat.st_ino}\n", ""
    ))
    assert broker._job_database(42) == test
    monkeypatch.setattr(broker, "_command", lambda argv, **_: subprocess.CompletedProcess(
        argv, 0, "unrecognized", ""
    ))
    with pytest.raises(broker.BrokerError, match="not an installed product"):
        broker._job_database(42)


def test_read_only_partial_recovery_stays_pending(recovery: Any) -> None:
    recovery.after_mount = lambda: setattr(recovery, "unsafe", True)
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(recovery.request, namespace_fd=42)
    assert next(iter(broker._load_leases().values()))["recovering"] == "1"
    recovery.unsafe = False
    recovery.after_mount = None
    assert broker.handle(recovery.request, namespace_fd=42)["ok"]
    assert recovery.mounts == 1


def test_old_unbound_preflight_cannot_recover(recovery: Any) -> None:
    leases = broker._load_leases()
    next(iter(leases.values())).pop("job_id")
    broker._save_leases(leases)
    with pytest.raises(broker.BrokerError, match="no longer authorized"):
        broker.handle(recovery.request, namespace_fd=42)
    assert recovery.mounts == recovery.unmounts == 0


def test_recovery_rejects_database_replacement(recovery: Any) -> None:
    leases = broker._load_leases()
    next(iter(leases.values()))["database"] = "different-inode"
    broker._save_leases(leases)
    with pytest.raises(broker.BrokerError, match="database identity changed"):
        broker.handle(recovery.request, namespace_fd=42)
    assert recovery.mounts == 0


def test_unreadable_mount_evidence_keeps_original_responsibility(
    recovery: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise broker.BrokerError("findmnt is unavailable")
    monkeypatch.setattr(broker, "_mounts", unreadable)
    before = broker._load_leases()
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(recovery.request, namespace_fd=42)
    assert broker._load_leases() == before
    assert recovery.mounts == 0
