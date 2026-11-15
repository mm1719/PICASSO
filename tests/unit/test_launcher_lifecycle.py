from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "host" / "usb_agent"))

from plankton_launcher import (
    HostAdapter,
    Launcher,
    LauncherError,
    Mode,
    ProcessIdentity,
    ProductionHost,
    SwitchRefused,
    Transaction,
    TransactionStore,
    _ProductionGui,
    _ProductionProgress,
)


@pytest.mark.parametrize("mode,selector,port", [("production", "current", 8000), ("test", "test-current", 18080)])
def test_gui_helper_matches_selected_runtime_release(mode: Mode, selector: str, port: int) -> None:
    with patch("plankton_launcher.subprocess.Popen") as popen:
        gui = ProductionHost().launch_gui(mode, "transaction")
    assert isinstance(gui, _ProductionGui)
    try:
        command = popen.call_args.args[0]
        assert command[:3] == (
            f"/opt/plankton-inference/{selector}/host/bin/plankton-web-app",
            f"http://127.0.0.1:{port}/",
            "--ready-fd",
        )
        assert popen.call_args.kwargs["pass_fds"] == (int(command[3]),)
    finally:
        os.close(gui.ready_fd)


def test_missing_release_helper_fails_without_global_fallback_and_closes_pipe() -> None:
    descriptors = os.pipe()
    with (
        patch("plankton_launcher.os.pipe", return_value=descriptors),
        patch("plankton_launcher.subprocess.Popen", side_effect=FileNotFoundError) as popen,
        pytest.raises(FileNotFoundError),
    ):
        ProductionHost().launch_gui("test", "transaction")
    assert popen.call_count == 1
    assert popen.call_args.args[0][0] == "/opt/plankton-inference/test-current/host/bin/plankton-web-app"
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_progress_helper_receives_launcher_owned_lifetime_pipe() -> None:
    inherited_fds: list[int] = []
    process = Mock()

    def start_helper(_command: tuple[str, ...], **kwargs: object) -> subprocess.Popen[str]:
        helper_fd = kwargs["pass_fds"][0]  # type: ignore[index]
        os.fstat(helper_fd)
        inherited_fds.append(helper_fd)
        return cast(subprocess.Popen[str], process)

    with patch("plankton_launcher.subprocess.Popen", side_effect=start_helper) as popen:
        progress = ProductionHost().start_progress("test", "transaction")
        assert isinstance(progress, _ProductionProgress)
        command = popen.call_args.args[0]
        assert command[:3] == (
            "/usr/local/sbin/plankton-switch-progress",
            "PICASSO Test",
            "--parent-lifetime-fd",
        )
        assert len(command) == 4
        helper_fd = int(command[3])
        assert inherited_fds == [helper_fd]
        assert popen.call_args.kwargs["pass_fds"] == (helper_fd,)
        with pytest.raises(OSError):
            os.fstat(helper_fd)
        assert progress.lifetime_fd is not None
        lifetime_fd = progress.lifetime_fd
        ProductionHost().close_progress(progress)
        assert process.wait.call_args.kwargs == {"timeout": 3}
        assert not process.terminate.called
        with pytest.raises(OSError):
            os.fstat(lifetime_fd)


class FakeGui:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class FakeProgress:
    pass


class FakeHost:
    def __init__(self, current: Mode | None = None) -> None:
        self.current = current
        self.events: list[tuple[str, str]] = []
        self.visible = True
        self.workload = False
        self.confirmed = True
        self.authenticated = True
        self.gui = FakeGui(4242)

    def current_mode(self) -> Mode | None:
        return self.current

    def runtime_healthy(self, _mode: Mode) -> bool:
        return True

    def has_active_workload(self) -> bool:
        self.events.append(("safety", "checked"))
        return self.workload

    def authenticate(self, mode: Mode) -> bool:
        self.events.append(("auth", mode))
        return self.authenticated

    def confirm_switch(self, current: Mode, target: Mode) -> bool:
        self.events.append(("confirm", f"{current}->{target}"))
        return self.confirmed

    def switch_runtime(self, mode: Mode) -> None:
        self.events.append(("switch", mode))
        self.current = mode

    def start_progress(self, mode: Mode, _transaction_id: str) -> FakeProgress:
        self.events.append(("progress", mode))
        return FakeProgress()

    def close_progress(self, _progress: FakeProgress) -> None:
        self.events.append(("progress", "closed"))

    def launch_gui(self, mode: Mode, _transaction_id: str) -> FakeGui:
        self.events.append(("launch", mode))
        return self.gui

    def process_identity(self, _gui: FakeGui, transaction_id: str) -> ProcessIdentity:
        return ProcessIdentity(self.gui.pid, os.getuid(), "/usr/bin/fake-gui", "1", transaction_id)

    def process_alive(self, identity: ProcessIdentity) -> bool:
        return identity.pid == self.gui.pid

    def wait_gui_visible(self, _gui: FakeGui, _timeout: float) -> bool:
        self.events.append(("visible", str(self.visible)))
        return self.visible

    def gui_exited(self, _gui: FakeGui) -> bool:
        self.events.append(("exit", "gui"))
        return True

    def focus_gui(self, _identity: ProcessIdentity) -> None:
        self.events.append(("focus", "existing"))

    def cleanup_gui(self, _identity: ProcessIdentity) -> None:
        self.events.append(("cleanup", "gui"))

    def audit(self, event: str, transaction_id: str, **_fields: str) -> None:
        self.events.append(("audit", f"{event}:{transaction_id}"))


def _store(tmp_path: Path) -> TransactionStore:
    return TransactionStore(tmp_path / "runtime")


def _committed(store: TransactionStore, mode: Mode = "production") -> None:
    store.runtime_dir.mkdir()
    store.save(
        Transaction(
            transaction_id="old-transaction",
            previous_mode=None,
            target_mode=mode,
            phase="GUI_VISIBLE",
            pid=4242,
            pid_uid=os.getuid(),
            pid_executable="/usr/bin/fake-gui",
            pid_start_time="1",
            commit_status="committed",
        )
    )


def test_confirmation_centering_uses_one_logical_move_without_physical_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TD-OPS-03: do not compare scaled list output with logical move input."""
    commands: list[tuple[str, ...]] = []
    window_rows = iter(
        (
            "0x03200004 0 241048 328 -18 633 325 workstation PICASSO runtime switch\n",
            "0x03200004 0 241048 1286 754 633 325 workstation PICASSO runtime switch\n",
            "0x03200004 0 241048 642 376 633 325 workstation PICASSO runtime switch\n",
        )
    )

    def fake_run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1] == "-lpG":
            stdout = next(window_rows)
        elif command[1] == "-d":
            stdout = "0 * DG: 1920x1080 VP: 0,0 WA: 66,40 1854x1040 Workspace 1\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert ProductionHost._center_confirmation_window(241048)
    assert [command for command in commands if command[1] == "-ir"] == [
        ("/usr/bin/wmctrl", "-ir", "0x03200004", "-e", "0,643,377,-1,-1"),
    ]


def test_success_commits_only_after_visible_gui_and_cleans_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    host = FakeHost()

    assert Launcher(cast(HostAdapter, host), store).run("test") == "closed"

    assert host.events.index(("visible", "True")) < host.events.index(("exit", "gui"))
    assert not store.record_path.exists()
    assert [event for event in host.events if event[0] == "switch"] == [("switch", "test")]


def test_authentication_cancel_preserves_runtime_without_owned_state(tmp_path: Path) -> None:
    """L1-01: cancelling graphical authentication is a side-effect-free no-op."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    host = FakeHost("production")
    host.authenticated = False

    assert Launcher(cast(HostAdapter, host), store).run("production") == "cancelled"

    assert host.current == "production"
    assert not store.record_path.exists()
    assert not any(event[0] in {"progress", "switch", "launch"} for event in host.events)


def test_rollback_gui_transaction_clears_when_restored_window_is_already_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    original_switch = host.switch_runtime

    def fail_test(mode: Mode) -> None:
        if mode == "test":
            raise LauncherError("target health failed")
        original_switch(mode)

    host.switch_runtime = fail_test  # type: ignore[method-assign]
    with pytest.raises(LauncherError, match="target health"):
        Launcher(cast(HostAdapter, host), store).run("test")
    assert store.load() is None
    assert any(event[0] == "audit" and event[1].startswith("gui_closed:") for event in host.events)

    host.authenticated = False
    assert Launcher(cast(HostAdapter, host), store).run("test") == "cancelled"
    assert store.load() is None


def test_authentication_failure_is_diagnostic_and_side_effect_free(tmp_path: Path) -> None:
    """L1-02: an adapter authentication failure remains diagnostic and secret-free."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    host = FakeHost("production")

    def fail_authentication(_mode: Mode) -> bool:
        raise LauncherError("authentication failed")

    host.authenticate = fail_authentication  # type: ignore[assignment]

    with pytest.raises(LauncherError, match="authentication failed"):
        Launcher(cast(HostAdapter, host), store).run("production")

    assert host.current == "production"
    assert not store.record_path.exists()
    assert not any(event[0] in {"progress", "switch", "launch"} for event in host.events)
    assert "secret" not in repr(host.events).lower()


def test_gui_failure_rolls_back_previous_runtime_before_commit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    host.visible = False

    with pytest.raises(LauncherError, match="GUI"):
        Launcher(cast(HostAdapter, host), store).run("test")

    assert ("switch", "test") in host.events
    assert any(event[0] == "audit" and event[1].startswith("rollback:") for event in host.events)
    assert not store.record_path.exists()
    assert host.current == "production"


def test_gui_visibility_timeout_uses_exact_limit_and_rolls_back_cleanly(tmp_path: Path) -> None:
    """L1-17: the process timeout uses its hard limit and preserves rollback cleanup."""
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    observed_timeouts: list[float] = []

    def time_out(_gui: FakeGui, timeout: float) -> bool:
        observed_timeouts.append(timeout)
        return len(observed_timeouts) > 1

    host.wait_gui_visible = time_out  # type: ignore[assignment]

    with pytest.raises(LauncherError, match="visible"):
        Launcher(cast(HostAdapter, host), store, gui_timeout=30).run("test")

    assert observed_timeouts == [30, 30]
    assert host.events.count(("cleanup", "gui")) == 2
    assert host.current == "production"
    assert ("launch", "production") in host.events
    assert store.load() is None
    assert any(event[0] == "audit" and event[1].startswith("gui_closed:") for event in host.events)


def test_runtime_switch_failure_restores_previous_runtime_and_gui(tmp_path: Path) -> None:
    """L1-06: a target runtime failure cannot strand the prior desktop state."""
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")

    def switch_runtime(mode: Mode) -> None:
        host.events.append(("switch", mode))
        if mode == "test":
            raise LauncherError("target runtime health failed")
        host.current = mode

    host.switch_runtime = switch_runtime  # type: ignore[method-assign]

    with pytest.raises(LauncherError, match="target runtime health failed"):
        Launcher(cast(HostAdapter, host), store).run("test")

    assert host.current == "production"
    assert ("launch", "production") in host.events
    assert store.load() is None


def test_failed_switch_tracks_restored_gui_until_normal_close(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    visible = threading.Event()
    close = threading.Event()

    def switch_runtime(mode: Mode) -> None:
        host.events.append(("switch", mode))
        if mode == "test":
            raise LauncherError("target runtime health failed")
        host.current = mode

    def gui_exited(_gui: FakeGui) -> bool:
        visible.set()
        return close.is_set()

    host.switch_runtime = switch_runtime  # type: ignore[method-assign]
    host.gui_exited = gui_exited  # type: ignore[method-assign]
    failure: list[BaseException] = []

    def launch() -> None:
        try:
            Launcher(cast(HostAdapter, host), store).run("test")
        except BaseException as error:  # noqa: BLE001 - capture worker-thread result
            failure.append(error)

    thread = threading.Thread(target=launch)
    thread.start()
    assert visible.wait(1)
    transaction = store.load()
    assert transaction is not None and transaction.target_mode == "production"
    assert transaction.commit_status == "committed"
    close.set()
    thread.join(1)
    assert not thread.is_alive()
    assert len(failure) == 1 and str(failure[0]) == "target runtime health failed"
    assert store.load() is None


def test_cancel_and_active_workload_do_not_start_a_switch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    host.confirmed = False

    assert Launcher(cast(HostAdapter, host), store).run("test") == "cancelled"
    assert not any(event[0] in {"auth", "switch", "launch"} for event in host.events)

    host.confirmed = True
    host.workload = True
    before = len(host.events)
    with pytest.raises(SwitchRefused, match="prevents switching"):
        Launcher(cast(HostAdapter, host), store).run("test")
    assert not any(event[0] == "confirm" for event in host.events[before:])


def test_concurrent_launcher_click_is_refused_before_any_side_effect(tmp_path: Path) -> None:
    """L1-14: the transaction lock serializes repeated launcher clicks."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    first_transaction = store.acquire()
    host = FakeHost("production")
    try:
        with pytest.raises(SwitchRefused, match="another launcher transaction"):
            Launcher(cast(HostAdapter, host), store).run("test")
    finally:
        store.release(first_transaction)

    assert host.events == []
    assert not store.record_path.exists()


def test_same_mode_focuses_recorded_gui_without_authentication(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")

    assert Launcher(cast(HostAdapter, host), store).run("production") == "focused"
    assert ("focus", "existing") in host.events
    assert not any(event[0] in {"auth", "switch", "launch"} for event in host.events)


def test_confirmed_opposite_mode_closes_old_gui_before_switch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")

    assert Launcher(cast(HostAdapter, host), store).run("test") == "closed"
    assert host.events.index(("cleanup", "gui")) < host.events.index(("switch", "test"))
    assert ("confirm", "production->test") in host.events


def test_replaced_transaction_does_not_make_old_launcher_fail_or_remove_new_record(
    tmp_path: Path,
) -> None:
    """TD-OPS-03: an opposite-mode takeover owns the record before old GUI exit."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    host = FakeHost()
    original_acquire = store.acquire
    acquisitions = 0

    def acquire() -> int:
        nonlocal acquisitions
        acquisitions += 1
        if acquisitions == 2:
            raise SwitchRefused("another launcher transaction is in progress")
        return original_acquire()

    replacement = Transaction(
        transaction_id="new-production-transaction",
        previous_mode="test",
        target_mode="production",
        phase="GUI_VISIBLE",
        pid=8888,
        pid_uid=os.getuid(),
        pid_executable="/usr/bin/fake-production-gui",
        pid_start_time="2",
        commit_status="committed",
    )

    def gui_replaced(_gui: FakeGui) -> bool:
        store.save(replacement)
        return True

    store.acquire = acquire  # type: ignore[method-assign]
    host.gui_exited = gui_replaced  # type: ignore[method-assign]

    assert Launcher(cast(HostAdapter, host), store).run("test") == "closed"
    assert store.load() == replacement


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_recoverable_signal_follows_commit_boundary(
    tmp_path: Path,
    signum: int,
    phase: str,
) -> None:
    """L1-10: recoverable signals roll back before commit and clean after it."""
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    launcher = Launcher(cast(HostAdapter, host), store)

    if phase == "before_commit":
        def interrupt_before_visible(_gui: FakeGui, _timeout: float) -> bool:
            launcher.handle_signal(signum)
            return True

        host.wait_gui_visible = interrupt_before_visible  # type: ignore[method-assign]
        with pytest.raises(LauncherError, match="visible"):
            launcher.run("test")
        assert host.current == "production"
        assert ("launch", "production") in host.events
    else:
        def interrupt_committed_gui(_gui: FakeGui) -> bool:
            launcher.handle_signal(signum)
            return False

        host.gui_exited = interrupt_committed_gui  # type: ignore[method-assign]
        assert launcher.run("test") == "interrupted"
        assert host.current == "test"

    if phase == "after_commit":
        assert ("cleanup", "gui") in host.events
    restored = store.load()
    if phase == "before_commit":
        assert restored is None
    else:
        assert restored is None


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
def test_precommit_signal_keeps_restored_gui_until_operator_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int,
) -> None:
    """A signal consumed by rollback must not also close the restored desktop."""
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    launcher = Launcher(cast(HostAdapter, host), store)
    restored_polls = 0
    original_switch = host.switch_runtime
    original_cleanup = host.cleanup_gui

    def no_restored_gui_cleanup(identity: ProcessIdentity) -> None:
        assert not any(event == ("launch", "production") for event in host.events), (
            "rollback-restored GUI must stay open until the operator closes it"
        )
        original_cleanup(identity)

    def signal_during_switch(mode: Mode) -> None:
        original_switch(mode)
        if mode == "test":
            launcher.handle_signal(signum)

    def restored_gui_exited(_gui: FakeGui) -> bool:
        nonlocal restored_polls
        restored_polls += 1
        record = store.load()
        assert record is not None and record.target_mode == "production"
        assert record.commit_status == "committed" and record.phase == "GUI_VISIBLE"
        # The original GUI may be closed for switching; the restored GUI may not.
        assert host.events.count(("cleanup", "gui")) == 1
        if restored_polls == 3:
            host.events.append(("operator", "close_restored_gui"))
            return True
        return False

    monkeypatch.setattr(host, "switch_runtime", signal_during_switch)
    monkeypatch.setattr(host, "cleanup_gui", no_restored_gui_cleanup)
    monkeypatch.setattr(host, "gui_exited", restored_gui_exited)
    monkeypatch.setattr("plankton_launcher.time.sleep", lambda _seconds: None)
    with pytest.raises(LauncherError, match="interrupted"):
        launcher.run("test")

    assert restored_polls == 3
    assert host.current == "production"
    assert ("operator", "close_restored_gui") in host.events
    assert host.events.count(("cleanup", "gui")) == 1
    assert not store.record_path.exists()


def test_new_signal_during_rollback_still_interrupts_restored_gui(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    launcher = Launcher(cast(HostAdapter, host), store)
    original_switch = host.switch_runtime

    def signal_during_rollback(mode: Mode) -> None:
        original_switch(mode)
        if mode == "test":
            launcher.handle_signal(signal.SIGHUP)
        elif mode == "production":
            launcher.handle_signal(signal.SIGTERM)

    host.switch_runtime = signal_during_rollback  # type: ignore[method-assign]
    host.gui_exited = lambda _gui: False  # type: ignore[method-assign]

    assert launcher.run("test") == "interrupted"

    assert host.current == "production"
    assert host.events.count(("cleanup", "gui")) == 2
    assert store.load() is None
    assert any(
        event[0] == "audit" and event[1].startswith("interrupted_after_commit:")
        for event in host.events
    )


def test_next_launch_reconciles_precommit_sigkill_state(tmp_path: Path) -> None:
    """L1-11: the next launch recovers a validated pre-commit SIGKILL record."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    store.save(
        Transaction(
            transaction_id="killed-transaction",
            previous_mode="production",
            target_mode="test",
            phase="GUI_STARTING",
            pid=4242,
            pid_uid=os.getuid(),
            pid_executable="/usr/bin/fake-gui",
            pid_start_time="1",
            commit_status="pending",
        )
    )
    host = FakeHost("test")

    assert Launcher(cast(HostAdapter, host), store).run("production") == "closed"

    rollback_audit = next(
        index
        for index, event in enumerate(host.events)
        if event[0] == "audit" and event[1].startswith("rollback_reconciled:")
    )
    assert host.events.index(("cleanup", "gui")) < rollback_audit
    assert host.events.index(("switch", "production")) < rollback_audit
    assert host.current == "production"
    assert not store.record_path.exists()


def test_reconciliation_never_cleans_a_reused_pid(tmp_path: Path) -> None:
    """L1-12: stale identity mismatch leaves an unrelated reused PID untouched."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    unrelated_pid = 9999
    store.save(
        Transaction(
            transaction_id="stale-transaction",
            previous_mode="production",
            target_mode="test",
            phase="GUI_STARTING",
            pid=unrelated_pid,
            pid_uid=os.getuid(),
            pid_executable="/usr/bin/fake-gui",
            pid_start_time="old-start-time",
            commit_status="pending",
        )
    )
    host = FakeHost("test")
    cleaned_pids: list[int] = []

    def process_alive(identity: ProcessIdentity) -> bool:
        return identity.pid == host.gui.pid and identity.start_time == "1"

    def cleanup_gui(identity: ProcessIdentity) -> None:
        cleaned_pids.append(identity.pid)

    host.process_alive = process_alive  # type: ignore[method-assign]
    host.cleanup_gui = cleanup_gui  # type: ignore[assignment]

    assert Launcher(cast(HostAdapter, host), store).run("production") == "closed"

    assert unrelated_pid not in cleaned_pids
    assert host.current == "production"
    assert not store.record_path.exists()


def test_transaction_record_rejects_secret_and_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    record = store.record_path
    record.write_text(json.dumps({"password": "secret"}), encoding="utf-8")
    record.chmod(0o600)
    with pytest.raises(LauncherError, match="unexpected fields"):
        store.load()

    record.unlink()
    target = tmp_path / "outside"
    target.write_text("do not touch", encoding="utf-8")
    record.symlink_to(target)
    with pytest.raises(LauncherError, match="regular file"):
        store.load()
    assert target.read_text(encoding="utf-8") == "do not touch"


@pytest.mark.parametrize("artifact", ["wrong_mode", "directory", "lock_symlink"])
def test_transaction_store_rejects_unsafe_artifact(
    tmp_path: Path,
    artifact: str,
) -> None:
    """L1-13: unsafe state artifacts fail closed without following their target."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("unchanged", encoding="utf-8")

    operation: Callable[[], object]
    if artifact == "wrong_mode":
        store.record_path.write_text("{}", encoding="utf-8")
        store.record_path.chmod(0o644)
        operation = store.load
    elif artifact == "directory":
        store.record_path.mkdir()
        operation = store.load
    else:
        store.lock_path.symlink_to(outside)
        operation = store.acquire

    with pytest.raises(LauncherError):
        operation()

    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_audit_is_phase_complete_transaction_stable_and_secret_free(tmp_path: Path) -> None:
    """L1-16: audit evidence carries phase/outcome/transaction without secrets."""
    store = _store(tmp_path)
    store.runtime_dir.mkdir()
    host = FakeHost()
    audits: list[tuple[str, str, dict[str, str]]] = []

    def audit(event: str, transaction_id: str, **fields: str) -> None:
        audits.append((event, transaction_id, fields))

    host.audit = audit  # type: ignore[method-assign]

    assert Launcher(cast(HostAdapter, host), store).run("test") == "closed"

    assert [event for event, _, _ in audits] == [
        "switch_started",
        "committed",
        "gui_closed",
    ]
    assert len({transaction_id for _, transaction_id, _ in audits}) == 1
    assert all(fields.get("phase") for _, _, fields in audits)
    serialized = json.dumps(audits, sort_keys=True).lower()
    assert not any(word in serialized for word in ("password", "credential", "secret", "token"))


def test_gui_disappearing_before_visibility_still_rolls_back(tmp_path: Path) -> None:
    """L1-07: a GUI exit race must not interrupt rollback of the old runtime."""
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")
    host.visible = False
    identity_calls = 0

    def process_identity(_gui: FakeGui, transaction_id: str) -> ProcessIdentity:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls > 1:
            raise LauncherError("process identity is unavailable")
        return ProcessIdentity(
            host.gui.pid,
            os.getuid(),
            "/usr/bin/fake-gui",
            "1",
            transaction_id,
        )

    host.process_identity = process_identity  # type: ignore[method-assign]

    with pytest.raises(LauncherError):
        Launcher(cast(HostAdapter, host), store).run("test")

    assert host.current == "production"
    assert ("launch", "production") in host.events
    assert not store.record_path.exists()


def test_gui_helper_early_exit_is_operator_readable_and_rolls_back(tmp_path: Path) -> None:
    """L1-07/L2-Extended: do not expose process-identity internals to the operator."""
    store = _store(tmp_path)
    _committed(store)
    host = FakeHost("production")

    def missing_identity(_gui: FakeGui, _transaction_id: str) -> ProcessIdentity:
        raise LauncherError("process identity is unavailable")

    host.process_identity = missing_identity  # type: ignore[assignment]

    with pytest.raises(LauncherError) as captured:
        Launcher(cast(HostAdapter, host), store).run("test")

    message = str(captured.value)
    assert "GUI" in message
    assert "identity" not in message
    assert host.current == "production"
    assert ("launch", "production") in host.events
    assert not store.record_path.exists()
