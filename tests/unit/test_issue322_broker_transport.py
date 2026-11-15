"""Connection failures are isolated without losing committed state or kernel FDs."""

from __future__ import annotations

import array
import errno
import json
import os
import signal
import socket
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Self, cast

import pytest

from host import usb_mount_broker as broker


@pytest.mark.parametrize("point", ["credentials", "receive", "reply"])
@pytest.mark.parametrize("error_type", [BrokenPipeError, ConnectionResetError, TimeoutError, OSError])
def test_transport_failure_preserves_commits_and_serves_next_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, error_type: type[OSError],
) -> None:
    handlers: dict[int, Callable[[int, object], None]] = {}
    replies: list[dict[str, object]] = []
    closed: list[int] = []
    fds: list[int] = []
    socket_path = tmp_path / "broker.sock"

    class Connection:
        def __init__(self, number: int) -> None:
            self.number = number

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            closed.append(self.number)

        def sendall(self, payload: bytes) -> None:
            if self.number == 1 and point == "reply":
                raise error_type(errno.ECONNRESET, "disconnected client")
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

    def credentials(connection: Connection) -> tuple[int, int]:
        if connection.number == 1 and point == "credentials":
            raise error_type(errno.ECONNRESET, "disconnected client")
        return 321, 1234

    def receive(connection: Connection) -> tuple[bytes, int]:
        if connection.number == 1 and point == "receive":
            raise error_type(errno.ECONNRESET, "disconnected client")
        fd = os.open(os.devnull, os.O_RDONLY)
        fds.append(fd)
        return json.dumps({"number": connection.number}).encode() + b"\n", fd

    def commit(request: dict[str, int], *, namespace_fd: int) -> dict[str, object]:
        assert namespace_fd in fds
        leases = broker._load_leases()
        leases[str(request["number"])] = {"lease_id": str(request["number"])}
        broker._save_leases(leases)
        return {"ok": True, "number": request["number"]}

    monkeypatch.setattr(broker, "SOCKET_PATH", socket_path)
    monkeypatch.setattr(broker, "LEASES_PATH", tmp_path / "leases.json")
    monkeypatch.setattr(broker, "MOUNT_GUARD_DIR", tmp_path / "guards")
    monkeypatch.setattr("host.usb_mount_broker.pwd.getpwnam", lambda _name: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr("host.usb_mount_broker.grp.getgrnam", lambda _name: SimpleNamespace(gr_gid=1234))
    monkeypatch.setattr("host.usb_mount_broker.os.chown", lambda *_args: None)
    monkeypatch.setattr("host.usb_mount_broker.signal.signal", handlers.__setitem__)
    monkeypatch.setattr("host.usb_mount_broker.socket.socket", lambda *_args: Listener())
    monkeypatch.setattr(broker, "_peer_credentials", credentials)
    monkeypatch.setattr(broker, "_receive_request", receive)
    monkeypatch.setattr(broker, "handle", commit)

    broker.serve()

    assert replies[-1] == {"ok": True, "number": 2}
    assert len(replies) == (1 if point == "reply" else 2)
    if point != "reply":
        assert replies[0] == {"ok": False, "error": "mount broker operation failed"}
    assert broker._load_leases() == (
        {"1": {"lease_id": "1"}, "2": {"lease_id": "2"}}
        if point == "reply" else {"2": {"lease_id": "2"}}
    )
    assert closed == [1, 2] and not socket_path.exists()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("flags,count", [(socket.MSG_CTRUNC, 1), (0, 2), (0, 1), (0, 0)])
def test_invalid_ancillary_message_closes_all_delivered_descriptors(flags: int, count: int) -> None:
    fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(count)]

    class Connection:
        def recvmsg(self, *_args: int) -> tuple[bytes, list[tuple[int, int, bytes]], int, None]:
            return b"{}\n", [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds).tobytes())], flags, None

    try:
        with pytest.raises(broker.BrokerError):
            broker._receive_request(cast(socket.socket, Connection()))
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
