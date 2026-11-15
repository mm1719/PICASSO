"""A disconnected request must not terminate the shared USB broker.

Only the transport and host boundary are faked; execute the real serve loop.
No host socket, USB, account, signal handler or service is modified.
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

from host import usb_mount_broker as broker


@pytest.mark.parametrize("failed_operation", [False, True])
@pytest.mark.parametrize("disconnected", [False, True])
def test_disconnected_reply_does_not_kill_broker_or_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_operation: bool, disconnected: bool,
) -> None:
    handlers: dict[int, Callable[[int, object], None]] = {}
    handled: list[int] = []
    replies: list[dict[str, object]] = []
    closed: list[int] = []
    namespace_fds: list[int] = []
    socket_path = tmp_path / "broker.sock"

    class Connection:
        def __init__(self, number: int) -> None:
            self.number = number

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            closed.append(self.number)

        def sendall(self, payload: bytes) -> None:
            if self.number == 1 and disconnected:
                raise BrokenPipeError("client already disconnected")
            replies.append(json.loads(payload))

    class Listener:
        accepted = 0

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def bind(self, path: str) -> None:
            assert path == str(socket_path)
            socket_path.touch()

        def listen(self, _backlog: int) -> None:
            pass

        def settimeout(self, _seconds: int) -> None:
            pass

        def accept(self) -> tuple[Connection, None]:
            self.accepted += 1
            if self.accepted <= 2:
                return Connection(self.accepted), None
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            raise TimeoutError

    def receive(connection: Connection) -> tuple[bytes, int]:
        namespace_fd = os.open(os.devnull, os.O_RDONLY)
        namespace_fds.append(namespace_fd)
        return json.dumps({"number": connection.number}).encode() + b"\n", namespace_fd

    def handle(request: dict[str, int], *, namespace_fd: int) -> dict[str, object]:
        assert namespace_fd in namespace_fds
        handled.append(request["number"])
        if failed_operation and request["number"] == 1:
            raise broker.BrokerError("native USB view was not restored")
        return {"ok": True, "number": request["number"]}

    monkeypatch.setattr(broker, "SOCKET_PATH", socket_path)
    monkeypatch.setattr(broker, "_reconcile_guard_markers", lambda: None)
    monkeypatch.setattr("host.usb_mount_broker.pwd.getpwnam", lambda _name: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr("host.usb_mount_broker.grp.getgrnam", lambda _name: SimpleNamespace(gr_gid=1234))
    monkeypatch.setattr("host.usb_mount_broker.os.chown", lambda *_args: None)
    monkeypatch.setattr("host.usb_mount_broker.signal.signal", handlers.__setitem__)
    monkeypatch.setattr("host.usb_mount_broker.socket.socket", lambda *_args: Listener())
    monkeypatch.setattr(broker, "_peer_credentials", lambda _connection: (321, 1234))
    monkeypatch.setattr(broker, "_receive_request", receive)
    monkeypatch.setattr(broker, "handle", handle)

    broker.serve()

    assert handled == [1, 2]
    expected_first = (
        {"ok": False, "error": "native USB view was not restored"}
        if failed_operation else {"ok": True, "number": 1}
    )
    assert replies == ([] if disconnected else [expected_first]) + [{"ok": True, "number": 2}]
    assert closed == [1, 2]
    assert not socket_path.exists()
    for namespace_fd in namespace_fds:
        with pytest.raises(OSError):
            os.fstat(namespace_fd)


@pytest.mark.parametrize("restore_fails", [False, True])
def test_failed_claim_and_failed_native_restore_keep_durable_responsibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_fails: bool,
) -> None:
    """A failed unmount may already remove the view; failed rollback is not idle."""
    ledger = tmp_path / "leases.json"
    ledger.write_text("{}\n")
    native = {Path("/media/operator/LABEL"): frozenset({"rw", "nodev", "nosuid"})}
    owned = tmp_path / "owned"
    request = {
        "operation": "claim", "block_device": "/dev/fake1", "drive_device": "/dev/fake",
        "uuid": "original-uuid", "serial": "original-serial", "consumer_id": "plankton-usb",
    }
    restores: list[tuple[str, str]] = []

    def unmount(_block: str) -> None:
        native.clear()
        raise broker.BrokerError("unmount timed out after native view disappeared")

    def restore(block: str, user: str) -> None:
        restores.append((block, user))
        if restore_fails:
            raise broker.BrokerError("native USB view was not restored")
        native[Path("/media/operator/LABEL")] = frozenset({"rw", "nodev", "nosuid"})

    def no_host_command(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("regression must not execute a host command")

    monkeypatch.setattr(broker, "LEASES_PATH", ledger)
    monkeypatch.setattr(broker, "MOUNT_GUARD_DIR", tmp_path / "guards")
    monkeypatch.setattr(broker, "_identity", lambda _request: None)
    monkeypatch.setattr(broker, "_validate_owned_path", lambda _path: owned)
    monkeypatch.setattr(broker, "_native_user", lambda _mounts: "operator")
    monkeypatch.setattr(
        broker, "_mounts", lambda _block, namespace_fd=None: native if namespace_fd is None else {},
    )
    monkeypatch.setattr(broker, "_diskseq", lambda _drive: "123")
    monkeypatch.setattr(broker, "_unmount_device", unmount)
    monkeypatch.setattr(broker, "_restore_native", restore)
    monkeypatch.setattr(broker, "_command", no_host_command)

    with pytest.raises(broker.BrokerError):
        broker.handle(request, namespace_fd=4242)

    assert restores == [("/dev/fake1", "operator")]
    if not restore_fails:
        assert native == {
            Path("/media/operator/LABEL"): frozenset({"rw", "nodev", "nosuid"}),
        }
        assert broker._lease_for(request) is None
        return
    assert native == {}
    # Freshly read the real private ledger, not an in-memory recovery promise.
    recovery = broker._lease_for(request)
    assert recovery is not None, "failed claim + failed rollback lost durable cleanup responsibility"
    assert all(recovery[key] == request[key] for key in ("uuid", "serial", "block_device", "drive_device"))
    assert recovery["native_user"] == "operator"
