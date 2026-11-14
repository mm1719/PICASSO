from __future__ import annotations

import fcntl
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from secrets import token_hex
from typing import Any, Literal, Protocol, cast
from urllib.request import urlopen

Mode = Literal["production", "test"]
CommitStatus = Literal["pending", "committed"]
RECORD_KEYS = frozenset(
    {
        "transaction_id",
        "previous_mode",
        "target_mode",
        "phase",
        "pid",
        "pid_uid",
        "pid_executable",
        "pid_start_time",
        "commit_status",
    }
)


class LauncherError(RuntimeError):
    pass


class SwitchRefused(LauncherError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    uid: int
    executable: str
    start_time: str
    transaction_id: str

    def as_record(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "pid_uid": self.uid,
            "pid_executable": self.executable,
            "pid_start_time": self.start_time,
        }


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_id: str
    previous_mode: Mode | None
    target_mode: Mode
    phase: str
    pid: int | None
    pid_uid: int | None
    pid_executable: str | None
    pid_start_time: str | None
    commit_status: CommitStatus

    def as_record(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "previous_mode": self.previous_mode,
            "target_mode": self.target_mode,
            "phase": self.phase,
            "pid": self.pid,
            "pid_uid": self.pid_uid,
            "pid_executable": self.pid_executable,
            "pid_start_time": self.pid_start_time,
            "commit_status": self.commit_status,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> Transaction:
        if set(value) != RECORD_KEYS:
            raise LauncherError("transaction record has unexpected fields")
        target = value["target_mode"]
        previous = value["previous_mode"]
        commit = value["commit_status"]
        if target not in {"production", "test"}:
            raise LauncherError("transaction target mode is invalid")
        if previous not in {None, "production", "test"}:
            raise LauncherError("transaction previous mode is invalid")
        if commit not in {"pending", "committed"}:
            raise LauncherError("transaction commit status is invalid")
        transaction_id = value["transaction_id"]
        phase = value["phase"]
        if not isinstance(transaction_id, str) or not transaction_id:
            raise LauncherError("transaction ID is invalid")
        if not isinstance(phase, str) or not phase:
            raise LauncherError("transaction phase is invalid")

        def optional_int(name: str) -> int | None:
            item = value[name]
            if item is None:
                return None
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise LauncherError(f"transaction {name} is invalid")
            return item

        def optional_text(name: str) -> str | None:
            item = value[name]
            if item is None:
                return None
            if not isinstance(item, str) or not item:
                raise LauncherError(f"transaction {name} is invalid")
            return item

        return cls(
            transaction_id=transaction_id,
            previous_mode=previous,
            target_mode=target,
            phase=phase,
            pid=optional_int("pid"),
            pid_uid=optional_int("pid_uid"),
            pid_executable=optional_text("pid_executable"),
            pid_start_time=optional_text("pid_start_time"),
            commit_status=commit,
        )


class TransactionStore:
    """User-owned lock and strict transaction record under XDG_RUNTIME_DIR."""

    def __init__(self, runtime_dir: Path | None = None) -> None:
        configured = runtime_dir or Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        self.runtime_dir = configured
        self.lock_path = configured / "picasso-launcher.lock"
        self.record_path = configured / "picasso-launcher.transaction.json"

    def _validate_parent(self) -> None:
        try:
            stat = self.runtime_dir.stat()
        except OSError as error:
            raise LauncherError("XDG_RUNTIME_DIR is unavailable") from error
        if not self.runtime_dir.is_dir() or stat.st_uid != os.getuid():
            raise LauncherError("XDG_RUNTIME_DIR is not user-owned")

    @staticmethod
    def _validate_file(path: Path) -> os.stat_result:
        try:
            stat = path.lstat()
        except FileNotFoundError as error:
            raise LauncherError("transaction file is absent") from error
        if path.is_symlink() or not path.is_file():
            raise LauncherError("transaction path is not a regular file")
        if stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o600:
            raise LauncherError("transaction file ownership or mode is invalid")
        return stat

    def acquire(self) -> int:
        self._validate_parent()
        existing = self.lock_path.exists() or self.lock_path.is_symlink()
        if existing:
            self._validate_file(self.lock_path)
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as error:
            raise LauncherError("launcher lock is unavailable") from error
        if not existing:
            os.fchmod(descriptor, 0o600)
        stat = os.fstat(descriptor)
        if stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o600:
            os.close(descriptor)
            raise LauncherError("launcher lock ownership or mode is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            raise SwitchRefused("another launcher transaction is in progress") from error
        return descriptor

    @staticmethod
    def release(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def load(self) -> Transaction | None:
        self._validate_parent()
        if not self.record_path.exists() and not self.record_path.is_symlink():
            return None
        self._validate_file(self.record_path)
        try:
            value = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LauncherError("transaction record is unreadable") from error
        if not isinstance(value, dict):
            raise LauncherError("transaction record is not an object")
        return Transaction.from_record(cast(Mapping[str, object], value))

    def save(self, transaction: Transaction) -> None:
        self._validate_parent()
        if self.record_path.exists() or self.record_path.is_symlink():
            self._validate_file(self.record_path)
        temporary = self.runtime_dir / f".picasso-launcher.{token_hex(8)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(transaction.as_record(), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.record_path)
            self._validate_file(self.record_path)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    def remove(self, transaction_id: str | None = None) -> None:
        existing = self.load()
        if existing is None:
            return
        if transaction_id is not None and existing.transaction_id != transaction_id:
            raise LauncherError("transaction ownership mismatch")
        self._validate_file(self.record_path)
        self.record_path.unlink()


class GuiHandle(Protocol):
    pass


class ProgressHandle(Protocol):
    pass


class HostAdapter(Protocol):
    def current_mode(self) -> Mode | None: ...

    def runtime_healthy(self, mode: Mode) -> bool: ...

    def has_active_workload(self) -> bool: ...

    def authenticate(self, mode: Mode) -> bool: ...

    def confirm_switch(self, current: Mode, target: Mode) -> bool: ...

    def switch_runtime(self, mode: Mode) -> None: ...

    def start_progress(self, mode: Mode, transaction_id: str) -> ProgressHandle: ...

    def close_progress(self, progress: ProgressHandle) -> None: ...

    def launch_gui(self, mode: Mode, transaction_id: str) -> GuiHandle: ...

    def process_identity(self, gui: GuiHandle, transaction_id: str) -> ProcessIdentity: ...

    def process_alive(self, identity: ProcessIdentity) -> bool: ...

    def wait_gui_visible(self, gui: GuiHandle, timeout: float) -> bool: ...

    def gui_exited(self, gui: GuiHandle) -> bool: ...

    def focus_gui(self, identity: ProcessIdentity) -> None: ...

    def cleanup_gui(self, identity: ProcessIdentity) -> None: ...

    def audit(self, event: str, transaction_id: str, **fields: str) -> None: ...


def _record_identity(transaction: Transaction) -> ProcessIdentity | None:
    values = (transaction.pid, transaction.pid_uid, transaction.pid_executable, transaction.pid_start_time)
    if any(value is None for value in values):
        return None
    return ProcessIdentity(
        pid=cast(int, transaction.pid),
        uid=cast(int, transaction.pid_uid),
        executable=cast(str, transaction.pid_executable),
        start_time=cast(str, transaction.pid_start_time),
        transaction_id=transaction.transaction_id,
    )


class Launcher:
    def __init__(
        self,
        host: HostAdapter,
        store: TransactionStore,
        *,
        gui_timeout: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.store = store
        self.gui_timeout = gui_timeout
        self.monotonic = monotonic
        self._interrupted = False
        self._active_transaction: Transaction | None = None

    def handle_signal(self, _signum: int, _frame: Any = None) -> None:
        self._interrupted = True

    def _reconcile(self, transaction: Transaction) -> None:
        identity = _record_identity(transaction)
        if transaction.commit_status == "committed":
            if (
                identity is None
                or not self.host.process_alive(identity)
                or not self.host.runtime_healthy(transaction.target_mode)
            ):
                if identity is not None and self.host.process_alive(identity):
                    self.host.cleanup_gui(identity)
                self.store.remove(transaction.transaction_id)
            return
        if identity is not None and self.host.process_alive(identity):
            self.host.cleanup_gui(identity)
        if transaction.previous_mode is not None:
            self.host.switch_runtime(transaction.previous_mode)
        self.host.audit("rollback_reconciled", transaction.transaction_id, phase=transaction.phase)
        self.store.remove(transaction.transaction_id)

    def _rollback(
        self,
        transaction: Transaction,
        gui: GuiHandle | None = None,
        *,
        restore_previous_gui: bool = False,
    ) -> tuple[GuiHandle, Transaction] | None:
        rollback_error: Exception | None = None
        restored_transaction: Transaction | None = None
        restored_gui: GuiHandle | None = None
        gui_identity: ProcessIdentity | None = None
        if gui is not None:
            try:
                gui_identity = self.host.process_identity(gui, transaction.transaction_id)
                self.host.cleanup_gui(gui_identity)
            except Exception:  # noqa: BLE001 - rollback must continue cleanup
                try:
                    exited = self.host.gui_exited(gui)
                except Exception:  # noqa: BLE001 - cleanup failure is reported below
                    exited = False
                if not exited:
                    rollback_error = LauncherError("GUI cleanup failed")
        recorded_identity = _record_identity(transaction)
        if (
            recorded_identity is not None
            and (
                gui is None
                or gui_identity is None
                or recorded_identity.pid != gui_identity.pid
            )
            and self.host.process_alive(recorded_identity)
        ):
            try:
                self.host.cleanup_gui(recorded_identity)
            except Exception as error:  # noqa: BLE001 - rollback must continue cleanup
                rollback_error = rollback_error or error
        if transaction.previous_mode is not None:
            try:
                self.host.switch_runtime(transaction.previous_mode)
                if restore_previous_gui:
                    restored_gui = self.host.launch_gui(
                        transaction.previous_mode, transaction.transaction_id
                    )
                    try:
                        restored_identity = self.host.process_identity(
                            restored_gui, transaction.transaction_id
                        )
                    except Exception as error:
                        raise LauncherError("previous GUI could not be restored") from error
                    if not self.host.wait_gui_visible(restored_gui, self.gui_timeout):
                        self.host.cleanup_gui(restored_identity)
                        raise LauncherError("previous GUI could not be restored")
                    restored_transaction = replace(
                        transaction,
                        previous_mode=None,
                        target_mode=transaction.previous_mode,
                        phase="GUI_VISIBLE",
                        commit_status="committed",
                        pid=restored_identity.pid,
                        pid_uid=restored_identity.uid,
                        pid_executable=restored_identity.executable,
                        pid_start_time=restored_identity.start_time,
                    )
            except Exception as error:  # noqa: BLE001 - rollback must continue cleanup
                rollback_error = rollback_error or error
        self.host.audit("rollback", transaction.transaction_id, phase=transaction.phase)
        if restored_transaction is not None and restored_gui is not None:
            self.store.save(restored_transaction)
            return restored_gui, restored_transaction
        else:
            self.store.remove(transaction.transaction_id)
        if rollback_error is not None:
            raise rollback_error
        return None

    def _install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        if threading.current_thread() is not threading.main_thread():
            return previous
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, self.handle_signal)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def run(self, target: Mode) -> str:
        previous_handlers = self._install_signal_handlers()
        deferred_error: BaseException | None = None
        try:
            descriptor = self.store.acquire()
            try:
                existing = self.store.load()
                if existing is not None:
                    self._reconcile(existing)
                current = self.host.current_mode()
                if current == target and existing is not None and existing.commit_status == "committed":
                    identity = _record_identity(existing)
                    if identity is not None and self.host.process_alive(identity):
                        self.host.focus_gui(identity)
                        self.host.audit("focus", existing.transaction_id, mode=target, phase=existing.phase)
                        return "focused"
                if current is not None and current != target:
                    if self.host.has_active_workload():
                        raise SwitchRefused("an active job or GPU workload prevents switching")
                    if not self.host.confirm_switch(current, target):
                        return "cancelled"
                if not self.host.authenticate(target):
                    return "cancelled"
                transaction = Transaction(
                    transaction_id=token_hex(16),
                    previous_mode=current,
                    target_mode=target,
                    phase="SWITCHING",
                    pid=None,
                    pid_uid=None,
                    pid_executable=None,
                    pid_start_time=None,
                    commit_status="pending",
                )
                self._active_transaction = transaction
                self.store.save(transaction)
                self.host.audit("switch_started", transaction.transaction_id, mode=target, phase=transaction.phase)
                gui: GuiHandle | None = None
                progress: ProgressHandle | None = None
                previous_gui = (
                    _record_identity(existing)
                    if existing is not None
                    and existing.commit_status == "committed"
                    and existing.target_mode == current
                    else None
                )
                try:
                    progress = self.host.start_progress(target, transaction.transaction_id)
                    if previous_gui is not None and self.host.process_alive(previous_gui):
                        self.host.cleanup_gui(previous_gui)
                    self.host.switch_runtime(target)
                    transaction = replace(transaction, phase="GUI_STARTING")
                    self.store.save(transaction)
                    if self._interrupted:
                        raise LauncherError("launch interrupted")
                    try:
                        gui = self.host.launch_gui(target, transaction.transaction_id)
                        identity = self.host.process_identity(gui, transaction.transaction_id)
                    except Exception as error:
                        raise LauncherError("GUI startup failed before visibility") from error
                    transaction = replace(
                        transaction,
                        phase="GUI_STARTING",
                        pid=identity.pid,
                        pid_uid=identity.uid,
                        pid_executable=identity.executable,
                        pid_start_time=identity.start_time,
                    )
                    self.store.save(transaction)
                    if self._interrupted:
                        raise LauncherError("target GUI launch interrupted before becoming visible")
                    if not self.host.wait_gui_visible(gui, self.gui_timeout):
                        raise LauncherError("target GUI did not become visible")
                    if self._interrupted:
                        raise LauncherError("target GUI launch interrupted before becoming visible")
                    transaction = replace(
                        transaction,
                        phase="GUI_VISIBLE",
                        commit_status="committed",
                    )
                    self.store.save(transaction)
                    self.host.audit("committed", transaction.transaction_id, mode=target, phase=transaction.phase)
                except BaseException as error:
                    restored = self._rollback(
                        transaction, gui, restore_previous_gui=previous_gui is not None
                    )
                    if restored is None:
                        raise
                    gui, transaction = restored
                    deferred_error = error
                finally:
                    if progress is not None:
                        try:
                            self.host.close_progress(progress)
                        except Exception:  # noqa: BLE001 - progress cleanup is best effort
                            self.host.audit(
                                "progress_cleanup_failed",
                                transaction.transaction_id,
                                phase=transaction.phase,
                            )
            finally:
                self.store.release(descriptor)
            if gui is None:
                return "started"
            identity = _record_identity(transaction)
            while not self.host.gui_exited(gui):
                if self._interrupted:
                    if identity is not None:
                        self.host.cleanup_gui(identity)
                    descriptor = self.store.acquire()
                    try:
                        self.store.remove(transaction.transaction_id)
                    finally:
                        self.store.release(descriptor)
                    self.host.audit(
                        "interrupted_after_commit",
                        transaction.transaction_id,
                        mode=target,
                        phase=transaction.phase,
                    )
                    return "interrupted"
                time.sleep(0.05)
            try:
                descriptor = self.store.acquire()
            except SwitchRefused:
                replacement = self.store.load()
                if replacement is None or replacement.transaction_id == transaction.transaction_id:
                    raise
                self.host.audit(
                    "gui_closed",
                    transaction.transaction_id,
                    mode=target,
                    phase=transaction.phase,
                    handoff="replaced",
                )
                return "closed"
            try:
                self.store.remove(transaction.transaction_id)
            finally:
                self.store.release(descriptor)
            self.host.audit(
                "gui_closed", transaction.transaction_id,
                mode=transaction.target_mode, phase=transaction.phase,
            )
            if deferred_error is not None:
                raise deferred_error
            return "closed"
        finally:
            self._active_transaction = None
            self._restore_signal_handlers(previous_handlers)


@dataclass(slots=True)
class _ProductionGui:
    process: subprocess.Popen[str]
    ready_fd: int


@dataclass(slots=True)
class _ProductionProgress:
    process: subprocess.Popen[str]


class ProductionHost:
    """Fixed host adapter; it has no test/fake mode route."""

    _urls: Mapping[Mode, str] = {
        "production": "http://127.0.0.1:8000/api/v1/health",
        "test": "http://127.0.0.1:18080/api/v1/health",
    }

    def current_mode(self) -> Mode | None:
        healthy: list[Mode] = [mode for mode in ("production", "test") if self.runtime_healthy(mode)]
        if len(healthy) > 1:
            raise LauncherError("both PICASSO runtimes are healthy")
        return healthy[0] if healthy else None

    def runtime_healthy(self, mode: Mode) -> bool:
        try:
            with urlopen(self._urls[mode], timeout=2) as response:
                return int(response.status) == 200
        except OSError:
            return False

    def has_active_workload(self) -> bool:
        if Path("/run/plankton-inference/active-job").exists():
            return True
        lock_path = Path("/run/plankton-inference/gpu.lock")
        try:
            descriptor = os.open(lock_path, os.O_RDWR)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def authenticate(self, mode: Mode) -> bool:
        state = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "plankton-picasso-askpass-state"
        state.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "SUDO_ASKPASS": "/usr/local/sbin/plankton-sudo-askpass",
                "SUDO_PROMPT": f"Authentication is required to start PICASSO{' Test' if mode == 'test' else ''}",
            }
        )
        result = subprocess.run(("/usr/bin/sudo", "-A", "-v"), env=environment, check=False)
        state.unlink(missing_ok=True)
        return result.returncode == 0

    def confirm_switch(self, current: Mode, target: Mode) -> bool:
        process = subprocess.Popen(
            (
                "/usr/bin/zenity",
                "--question",
                "--title=PICASSO runtime switch",
                f"--text={current} is active. Close it and switch to {target}?",
                "--ok-label=Close and switch",
                "--cancel-label=Cancel",
                "--default-cancel",
            ),
        )
        deadline = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < deadline:
            if self._center_confirmation_window(process.pid):
                break
            time.sleep(0.02)
        return process.wait() == 0

    @staticmethod
    def _center_confirmation_window(pid: int) -> bool:
        def locate_window(output: str) -> tuple[str, int, int, int, int] | None:
            for line in output.splitlines():
                # wmctrl -lpG: window, desktop, pid, x, y, width, height, host, title.
                fields = line.split(None, 8)
                if len(fields) < 9 or fields[2] != str(pid) or fields[8] != "PICASSO runtime switch":
                    continue
                window_id = fields[0]
                if re.fullmatch(r"0x[0-9a-fA-F]+", window_id) is None:
                    continue
                try:
                    x, y, width, height = (int(fields[index]) for index in range(3, 7))
                except ValueError:
                    continue
                if width <= 0 or height <= 0:
                    continue
                return window_id, x, y, width, height
            return None

        windows = subprocess.run(
            ("/usr/bin/wmctrl", "-lpG"), capture_output=True, text=True, check=False
        )
        window = locate_window(windows.stdout)
        if window is None:
            return False
        window_id, _, _, width, height = window
        desktops = subprocess.run(("/usr/bin/wmctrl", "-d"), capture_output=True, text=True, check=False)
        active_desktop = next(
            (desktop for desktop in desktops.stdout.splitlines() if re.search(r"^\s*\d+\s+\*\s", desktop)),
            None,
        )
        if active_desktop is None:
            return False
        geometry = re.search(r"\bDG:\s*(\d+)x(\d+)\b", active_desktop)
        viewport = re.search(r"\bVP:\s*([+-]?\d+)\s*,\s*([+-]?\d+)\b", active_desktop)
        if geometry is None or viewport is None:
            return False
        root_width, root_height = int(geometry.group(1)), int(geometry.group(2))
        root_x, root_y = int(viewport.group(1)), int(viewport.group(2))
        target_x = root_x + (root_width - width) // 2
        target_y = root_y + (root_height - height) // 2

        result = subprocess.run(
            ("/usr/bin/wmctrl", "-ir", window_id, "-e", f"0,{target_x},{target_y},-1,-1"),
            check=False,
        )
        return result.returncode == 0

    def switch_runtime(self, mode: Mode) -> None:
        result = subprocess.run(
            ("/usr/bin/sudo", "-n", "/usr/local/sbin/plankton-operator", "runtime", mode),
            check=False,
        )
        if result.returncode != 0:
            raise LauncherError(f"failed to switch runtime to {mode}")

    def start_progress(self, mode: Mode, _transaction_id: str) -> ProgressHandle:
        label = "PICASSO Test" if mode == "test" else "PICASSO"
        return _ProductionProgress(
            subprocess.Popen(
                ("/usr/local/sbin/plankton-switch-progress", label),
                close_fds=True,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    def close_progress(self, progress: ProgressHandle) -> None:
        if not isinstance(progress, _ProductionProgress):
            raise LauncherError("production adapter received an unknown progress handle")
        if progress.process.poll() is None:
            progress.process.terminate()
        try:
            progress.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            progress.process.kill()
            progress.process.wait()

    def launch_gui(self, mode: Mode, _transaction_id: str) -> GuiHandle:
        selector = "test-current" if mode == "test" else "current"
        helper = f"/opt/plankton-inference/{selector}/host/bin/plankton-web-app"
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        url = "http://127.0.0.1:18080/" if mode == "test" else "http://127.0.0.1:8000/"
        try:
            process = subprocess.Popen(
                (helper, url, "--ready-fd", str(write_fd)),
                pass_fds=(write_fd,),
                close_fds=True,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        return _ProductionGui(process, read_fd)

    def process_identity(self, gui: GuiHandle, transaction_id: str) -> ProcessIdentity:
        if not isinstance(gui, _ProductionGui):
            raise LauncherError("production adapter received an unknown GUI handle")
        return _linux_identity(gui.process.pid, transaction_id)

    def process_alive(self, identity: ProcessIdentity) -> bool:
        try:
            current = _linux_identity(identity.pid, identity.transaction_id)
        except LauncherError:
            return False
        return current == identity

    def wait_gui_visible(self, gui: GuiHandle, timeout: float) -> bool:
        if not isinstance(gui, _ProductionGui):
            raise LauncherError("production adapter received an unknown GUI handle")
        ready, _, _ = select.select((gui.ready_fd,), (), (), timeout)
        try:
            return bool(ready and os.read(gui.ready_fd, 64).startswith(b"visible"))
        finally:
            os.close(gui.ready_fd)

    def gui_exited(self, gui: GuiHandle) -> bool:
        if not isinstance(gui, _ProductionGui):
            raise LauncherError("production adapter received an unknown GUI handle")
        return gui.process.poll() is not None

    def focus_gui(self, identity: ProcessIdentity) -> None:
        result = subprocess.run(("/usr/bin/wmctrl", "-lp"), capture_output=True, text=True, check=False)
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[2] == str(identity.pid):
                subprocess.run(("/usr/bin/wmctrl", "-ia", fields[0]), check=False)
                return
        raise LauncherError("PICASSO window is not present")

    def cleanup_gui(self, identity: ProcessIdentity) -> None:
        if not self.process_alive(identity):
            return
        os.kill(identity.pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while self.process_alive(identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.process_alive(identity):
            os.kill(identity.pid, signal.SIGKILL)

    def audit(self, event: str, transaction_id: str, **fields: str) -> None:
        details = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        subprocess.run(
            ("/usr/bin/logger", "-t", "plankton-launcher", "--", f"event={event} transaction={transaction_id} {details}"),
            check=False,
        )


def _linux_identity(pid: int, transaction_id: str) -> ProcessIdentity:
    proc = Path("/proc") / str(pid)
    try:
        stat = proc.stat()
        executable = os.readlink(proc / "exe")
        fields = (proc / "stat").read_text(encoding="ascii").split()
        start_time = fields[21]
    except (OSError, IndexError, UnicodeError) as error:
        raise LauncherError("process identity is unavailable") from error
    return ProcessIdentity(pid, stat.st_uid, executable, start_time, transaction_id)


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else list(sys.argv[1:])
    if len(arguments) != 1 or arguments[0] not in {"production", "test"}:
        print("usage: plankton-desktop-launcher production | test", file=sys.stderr)
        return 2
    target = cast(Mode, arguments[0])
    try:
        result = Launcher(ProductionHost(), TransactionStore()).run(target)
    except SwitchRefused as error:
        print(f"PICASSO switch refused: {error}", file=sys.stderr)
        return 1
    except LauncherError as error:
        print(f"PICASSO launcher failed: {error}", file=sys.stderr)
        return 1
    if result == "cancelled":
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
