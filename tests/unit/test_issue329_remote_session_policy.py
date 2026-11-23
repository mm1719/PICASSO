"""No login/session/GUI action is performed; exercise the real local-session guard."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from host import usb_mount_broker as broker


@pytest.mark.parametrize("remote", ["yes", "no", ""])
def test_only_trusted_local_session_can_run_fixed_nautilus_close(
    monkeypatch: pytest.MonkeyPatch, remote: str,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "host.usb_mount_broker.pwd.getpwnam", lambda _user: SimpleNamespace(pw_uid=1000),
    )
    monkeypatch.setattr(Path, "is_socket", lambda path: path == Path("/run/user/1000/bus"))
    monkeypatch.setattr(broker, "_wait_for_nautilus_release", lambda *_args: True)

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ("/usr/bin/loginctl", "show-user"):
            return subprocess.CompletedProcess(argv, 0, "session-1\n", "")
        if argv[:2] == ("/usr/bin/loginctl", "show-session"):
            return subprocess.CompletedProcess(
                argv, 0, f"User=1000\nActive=yes\nRemote={remote}\nState=active\nType=wayland\n", "",
            )
        assert argv == (
            "/usr/bin/systemd-run", "--user", "--machine=operator@.host", "--wait", "--pipe",
            "--quiet", "/usr/bin/nautilus", "--quit",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(broker, "_run", run)
    if remote == "no":
        broker._close_nautilus("/dev/fake1", "operator")
    else:
        with pytest.raises(broker.BrokerError, match="native mount is busy"):
            broker._close_nautilus("/dev/fake1", "operator")
    assert sum(call[0] == "/usr/bin/systemd-run" for call in calls) == (remote == "no")


@pytest.mark.parametrize("sessions", ["local remote", "remote local", "local other-local"])
def test_per_user_close_refuses_ambiguous_initiator_even_when_a_local_session_exists(
    monkeypatch: pytest.MonkeyPatch, sessions: str,
) -> None:
    closes: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "host.usb_mount_broker.pwd.getpwnam", lambda _user: SimpleNamespace(pw_uid=1000),
    )
    monkeypatch.setattr(Path, "is_socket", lambda path: path == Path("/run/user/1000/bus"))
    monkeypatch.setattr(broker, "_wait_for_nautilus_release", lambda *_args: True)

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ("/usr/bin/loginctl", "show-user"):
            return subprocess.CompletedProcess(argv, 0, sessions, "")
        if argv[:2] == ("/usr/bin/loginctl", "show-session"):
            remote = "yes" if argv[2] == "remote" else "no"
            return subprocess.CompletedProcess(
                argv, 0, f"User=1000\nActive=yes\nRemote={remote}\nState=active\nType=wayland\n", "",
            )
        closes.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(broker, "_run", run)
    with pytest.raises(broker.BrokerError, match="native mount is busy"):
        broker._close_nautilus("/dev/fake1", "operator")
    assert closes == []
