"""Failed-claim cleanup must not act on a replacement at the same device node."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from host import usb_mount_broker as broker


@pytest.mark.parametrize("replacement", ["unchanged", "filesystem", "hardware"])
def test_failed_claim_revalidates_device_before_native_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    request = {
        "operation": "claim", "block_device": "/dev/fake1", "drive_device": "/dev/fake",
        "uuid": "original-uuid", "serial": "original-serial", "consumer_id": "plankton-usb",
    }
    live = {"uuid": request["uuid"], "serial": request["serial"], "diskseq": "10"}
    native_path = Path("/media/operator/LABEL")
    native = {native_path: frozenset({"rw", "nodev", "nosuid", "noexec"})}
    restored: list[tuple[str, str]] = []
    owned_root = tmp_path / "owned"
    owned_root.mkdir()

    def command(
        argv: tuple[str, ...], *, namespace_fd: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert namespace_fd is None
        assert argv[0] == "/usr/bin/lsblk", "no real host commands are allowed"
        column = argv[argv.index("--output") + 1]
        assert column in {"UUID,SERIAL", "SERIAL"}
        output = (
            f"{live['uuid']} {live['serial']}\n" if column == "UUID,SERIAL"
            else f"{live['serial']}\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    def detach(_block: str) -> None:
        # lsblk validated the original device before this slow action. The same
        # node is reused before rollback; immutable request fields are not proof.
        native.clear()
        if replacement != "unchanged":
            live["uuid" if replacement == "filesystem" else "serial"] = "replacement"
            live["diskseq"] = "11"
        raise broker.BrokerError("native detach timed out after device replacement")

    def restore(block: str, user: str) -> None:
        restored.append((block, user))
        native[native_path] = frozenset({"rw", "nodev", "nosuid", "noexec"})

    monkeypatch.setattr(broker, "LEASES_PATH", tmp_path / "leases.json")
    monkeypatch.setattr(broker, "MOUNT_GUARD_DIR", tmp_path / "guards")
    monkeypatch.setattr(broker, "SERVICE_ROOT", owned_root)
    monkeypatch.setattr(broker, "_command", command)
    monkeypatch.setattr(broker, "_diskseq", lambda _drive: live["diskseq"])
    monkeypatch.setattr(
        broker, "_mounts", lambda _block, namespace_fd=None: dict(native) if namespace_fd is None else {},
    )
    monkeypatch.setattr(broker, "_unmount_device", detach)
    monkeypatch.setattr(broker, "_restore_native", restore)

    with pytest.raises(broker.BrokerError):
        broker.handle(request, namespace_fd=4242)

    pending = broker._lease_for(request)
    if replacement == "unchanged":
        assert restored == [("/dev/fake1", "operator")]
        assert native == {native_path: frozenset({"rw", "nodev", "nosuid", "noexec"})}
        assert pending is None
        return
    assert restored == [], f"rollback touched replacement; original cleanup record is {pending!r}"
    assert native == {}, "replacement must remain untouched"
    assert pending is not None, "the original device's unresolved cleanup must remain durable"
    assert all(pending[key] == request[key] for key in ("uuid", "serial", "block_device", "drive_device"))
    assert pending["native_user"] == "operator"
