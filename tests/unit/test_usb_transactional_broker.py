from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from host.usb_mount_broker import (
    BrokerError,
    _guard_acquire,
    _guard_marker,
    _release,
    _unmount_device,
    handle,
)


def test_busy_unmount_retries_after_nautilus_action_without_fuser_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_mount = Path("/media/operator/LABEL")
    command_calls: list[tuple[str, ...]] = []
    action_calls: list[tuple[str, ...]] = []

    def command(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command_calls.append(argv)
        if len(command_calls) == 1:
            raise BrokerError("udisksctl failed: already mounted or mount point busy")
        return subprocess.CompletedProcess(argv, 0, "unmounted", "")

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        action_calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", "action completed")

    monkeypatch.setattr(
        "host.usb_mount_broker._mounts",
        lambda _block: {native_mount: frozenset({"rw", "nodev", "nosuid", "noexec"})},
    )
    monkeypatch.setattr("host.usb_mount_broker._active_local_session", lambda *_args: True)
    monkeypatch.setattr("host.usb_mount_broker._nautilus_holds", lambda *_args: False)
    monkeypatch.setattr("host.usb_mount_broker._command", command)
    monkeypatch.setattr("host.usb_mount_broker._run", run)

    _unmount_device("/dev/fake1")

    assert len(command_calls) == 2
    assert action_calls == [
        (
            "/usr/bin/systemd-run",
            "--user",
            "--machine=operator@.host",
            "--wait",
            "--pipe",
            "--quiet",
            "/usr/bin/nautilus",
            "--quit",
        )
    ]


def test_busy_unmount_waits_for_delayed_nautilus_convergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_mount = Path("/media/operator/LABEL")
    events: list[str] = []
    command_calls = 0

    def command(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal command_calls
        command_calls += 1
        if command_calls in {1, 2}:
            raise BrokerError("udisksctl failed: already mounted or mount point busy")
        assert events[-1] == "holder-released"
        return subprocess.CompletedProcess(argv, 0, "unmounted", "")

    holder_states = iter((True, True, False))

    def holder(_block: str, _user: str) -> bool:
        state = next(holder_states)
        events.append("holder-present" if state else "holder-released")
        return state

    monkeypatch.setattr(
        "host.usb_mount_broker._mounts",
        lambda _block: {native_mount: frozenset({"rw", "nodev", "nosuid", "noexec"})},
    )
    monkeypatch.setattr("host.usb_mount_broker._active_local_session", lambda *_args: True)
    monkeypatch.setattr("host.usb_mount_broker._nautilus_holds", holder)
    monkeypatch.setattr("host.usb_mount_broker._command", command)

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        events.append("action")
        return subprocess.CompletedProcess(argv, 255, "", "done")

    monkeypatch.setattr(
        "host.usb_mount_broker._run",
        run,
    )
    monkeypatch.setattr("host.usb_mount_broker.time.sleep", lambda _delay: None)

    _unmount_device("/dev/fake1")

    assert command_calls == 3
    assert events == ["action", "holder-present", "holder-present", "holder-released"]


def _claim_request() -> dict[str, str]:
    return {
        "operation": "claim",
        "block_device": "/dev/fake1",
        "drive_device": "/dev/fake",
        "uuid": "uuid",
        "serial": "serial",
        "consumer_id": "plankton-usb",
    }


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"native_user": "operator"}, "native restoration authority is lease-bound"),
        ({"owned_root": "/media/plankton-usb/uuid"}, "claim inputs must be broker-derived"),
        ({"lease_id": "lease", "generation": "1"}, "claim inputs must be broker-derived"),
    ],
)
def test_claim_rejects_caller_selected_transition_authority(
    values: dict[str, str], message: str
) -> None:
    request = {**_claim_request(), **values}

    with pytest.raises(BrokerError, match=message):
        handle(request)


def test_release_requires_the_opaque_persisted_lease() -> None:
    request = {**_claim_request(), "operation": "release"}

    with pytest.raises(BrokerError, match="opaque ownership lease is required"):
        handle(request)


def test_failed_claim_restores_native_view_before_returning_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_mount = Path("/media/operator/LABEL")
    owned = tmp_path / "owned"
    host_mounts = {native_mount: frozenset({"rw", "nodev", "nosuid"})}
    restored: list[tuple[str, str]] = []

    monkeypatch.setattr("host.usb_mount_broker._identity", lambda _request: None)
    monkeypatch.setattr("host.usb_mount_broker._lease_for", lambda _request: None)
    monkeypatch.setattr("host.usb_mount_broker._validate_owned_path", lambda _path: owned)
    monkeypatch.setattr("host.usb_mount_broker._native_user", lambda _mounts: "operator")
    monkeypatch.setattr(
        "host.usb_mount_broker._mounts",
        lambda _block, namespace_fd=None: host_mounts if namespace_fd is None else {},
    )
    monkeypatch.setattr("host.usb_mount_broker._unmount_device", lambda _block: host_mounts.clear())
    monkeypatch.setattr("host.usb_mount_broker.pwd.getpwnam", lambda _name: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr("host.usb_mount_broker.grp.getgrnam", lambda _name: SimpleNamespace(gr_gid=1234))
    monkeypatch.setattr("host.usb_mount_broker.os.chown", lambda *_args: None)
    monkeypatch.setattr(
        "host.usb_mount_broker._mount_owned",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BrokerError("mount failed")),
    )
    monkeypatch.setattr(
        "host.usb_mount_broker._restore_native",
        lambda block, user: restored.append((block, user)),
    )

    with pytest.raises(BrokerError, match="mount failed"):
        handle(_claim_request(), namespace_fd=4242)

    assert restored == [("/dev/fake1", "operator")]
    assert not owned.exists()


def test_reconcile_releases_durable_lease_in_authenticated_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {**_claim_request(), "operation": "reconcile"}
    lease = {"lease_id": "lease", "generation": "1"}
    owned = Path("/media/plankton-usb/uuid")
    released: list[tuple[dict[str, str], Path, dict[str, str], int | None]] = []

    monkeypatch.setattr("host.usb_mount_broker._identity", lambda _request: None)
    monkeypatch.setattr("host.usb_mount_broker._lease_for", lambda _request: lease)
    monkeypatch.setattr("host.usb_mount_broker._check_lease", lambda _request, _lease: owned)

    def release(
        current_request: dict[str, str],
        current_owned: Path,
        *,
        lease: dict[str, str],
        namespace_fd: int | None,
    ) -> dict[str, object]:
        released.append((current_request, current_owned, lease, namespace_fd))
        return {"ok": True, "released": True}

    monkeypatch.setattr("host.usb_mount_broker._release", release)

    assert handle(request, namespace_fd=4242) == {"ok": True, "released": True}
    assert released == [
        (
            {
                **request,
                "owned_root": "",
                "lease_id": "",
                "generation": "",
                "native_user": "",
                "job_id": "",
            },
            owned,
            lease,
            4242,
        )
    ]


def test_release_finds_same_identity_lease_after_block_path_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        **_claim_request(),
        "operation": "release",
        "block_device": "/dev/reinserted1",
        "drive_device": "/dev/reinserted",
        "lease_id": "lease",
        "generation": "1",
    }
    lease = {
        "block_device": "/dev/fake1",
        "drive_device": "/dev/fake",
        "uuid": "uuid",
        "serial": "serial",
        "consumer_id": "plankton-usb",
        "owned_root": "/media/plankton-usb/uuid",
        "lease_id": "lease",
        "generation": "1",
        "native_user": "operator",
    }
    old_key = "/dev/fake1\0uuid\0serial"

    monkeypatch.setattr("host.usb_mount_broker._identity", lambda _request: None)
    monkeypatch.setattr("host.usb_mount_broker._load_leases", lambda: {old_key: lease})
    monkeypatch.setattr(
        "host.usb_mount_broker._check_lease",
        lambda _request, _lease: Path("/media/plankton-usb/uuid"),
    )
    monkeypatch.setattr(
        "host.usb_mount_broker._release",
        lambda *_args, **_kwargs: {"ok": True, "released": True},
    )

    assert handle(request, namespace_fd=4242) == {"ok": True, "released": True}


def test_release_after_reenumeration_accepts_native_rw_without_noexec_and_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_block = "/dev/fake1"
    current_block = "/dev/reinserted1"
    old_key = f"{old_block}\0uuid\0serial"
    lease = {
        "block_device": old_block,
        "drive_device": "/dev/fake",
        "uuid": "uuid",
        "serial": "serial",
        "consumer_id": "plankton-usb",
        "owned_root": "/media/plankton-usb/uuid",
        "lease_id": "lease",
        "generation": "1",
        "native_user": "operator",
    }
    request = {
        **_claim_request(),
        "operation": "release",
        "block_device": current_block,
        "drive_device": "/dev/reinserted",
        "lease_id": "lease",
        "generation": "1",
    }
    leases_path = tmp_path / "usb-ownership-leases.json"
    leases_path.write_text(json.dumps({old_key: lease}), encoding="utf-8")
    monkeypatch.setattr("host.usb_mount_broker.LEASES_PATH", leases_path)
    monkeypatch.setattr("host.usb_mount_broker.MOUNT_GUARD_DIR", tmp_path / "guards")
    _guard_acquire(old_block)
    _guard_acquire(current_block)

    native = {
        Path("/media/operator/LABEL"): frozenset({"rw", "nodev", "nosuid", "showexec", "relatime"})
    }
    monkeypatch.setattr("host.usb_mount_broker._identity", lambda _request: None)
    monkeypatch.setattr(
        "host.usb_mount_broker._mounts",
        lambda _block, namespace_fd=None: {} if namespace_fd is not None else native,
    )
    monkeypatch.setattr("host.usb_mount_broker.pwd.getpwnam", lambda _user: object())
    monkeypatch.setattr(
        "host.usb_mount_broker._command",
        lambda _argv: pytest.fail("an existing safe native RW mount must not be remounted"),
    )

    assert handle(request, namespace_fd=4242) == {"ok": True, "released": True}
    assert json.loads(leases_path.read_text(encoding="utf-8")) == {}
    assert not _guard_marker(old_block).exists()
    assert not _guard_marker(current_block).exists()


def test_release_retains_reconciliation_when_native_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    controlled = {owned: frozenset({"rw", "nodev", "nosuid", "noexec"})}
    saved: list[dict[str, dict[str, str]]] = []

    monkeypatch.setattr(
        "host.usb_mount_broker._mounts",
        lambda _block, namespace_fd=None: controlled if namespace_fd is not None else {},
    )
    monkeypatch.setattr(
        "host.usb_mount_broker._unmount", lambda path, **_values: controlled.pop(path)
    )
    monkeypatch.setattr(
        "host.usb_mount_broker._restore_native",
        lambda *_args: (_ for _ in ()).throw(BrokerError("native USB view was not restored")),
    )
    monkeypatch.setattr("host.usb_mount_broker._save_leases", saved.append)

    with pytest.raises(BrokerError, match="native USB view was not restored"):
        _release(
            {"block_device": "/dev/fake1", "uuid": "uuid", "serial": "serial"},
            owned,
            lease={"native_user": "operator"},
            namespace_fd=4242,
        )

    assert controlled == {}
    assert saved == []


def test_release_restores_native_before_finalizing_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("host.usb_mount_broker.MOUNT_GUARD_DIR", tmp_path / "guard")
    owned = tmp_path / "owned"
    owned.mkdir()
    controlled = {owned: frozenset({"rw", "nodev", "nosuid", "noexec"})}
    lease_key = "/dev/fake1\0uuid\0serial"
    leases = {lease_key: {"lease_id": "lease"}}
    events: list[str] = []
    saved: list[dict[str, dict[str, str]]] = []

    monkeypatch.setattr(
        "host.usb_mount_broker._mounts",
        lambda _block, namespace_fd=None: controlled if namespace_fd is not None else {},
    )
    monkeypatch.setattr(
        "host.usb_mount_broker._unmount", lambda path, **_values: controlled.pop(path)
    )
    monkeypatch.setattr(
        "host.usb_mount_broker._restore_native",
        lambda *_args: events.append("native-restored"),
    )
    monkeypatch.setattr("host.usb_mount_broker._load_leases", lambda: leases)

    def save(value: dict[str, dict[str, str]]) -> None:
        events.append("lease-finalized")
        saved.append(value.copy())

    monkeypatch.setattr("host.usb_mount_broker._save_leases", save)

    result = _release(
        {"block_device": "/dev/fake1", "uuid": "uuid", "serial": "serial"},
        owned,
        lease={"native_user": "operator"},
        namespace_fd=4242,
    )

    assert result == {"ok": True, "released": True}
    assert events == ["native-restored", "lease-finalized"]
    assert saved == [{}]
