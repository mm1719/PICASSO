from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
PROGRESS_HELPER = REPOSITORY_ROOT / "host" / "bin" / "plankton-switch-progress"


def _fake_gtk(tmp_path: Path) -> Path:
    package = tmp_path / "gi"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "def require_version(_name, _version): pass\n", encoding="utf-8"
    )
    (package / "repository.py").write_text(
        """
import os
import select

class GLib:
    IO_IN, IO_HUP, IO_ERR = 1, 2, 4
    watch = None
    @classmethod
    def io_add_watch(cls, fd, _conditions, callback):
        cls.watch = (fd, callback)
        return 1
    @staticmethod
    def timeout_add(_milliseconds, _callback): return 1

class _Window:
    def __init__(self, **_kwargs): pass
    def set_modal(self, *_args): pass
    def set_deletable(self, *_args): pass
    def set_resizable(self, *_args): pass
    def set_default_size(self, *_args): pass
    def set_position(self, *_args): pass
    def connect(self, *_args): pass
    def add(self, *_args): pass
    def show_all(self): pass
    def present(self):
        marker = os.environ.get("FAKE_GTK_SHOWN")
        if marker:
            open(marker, "w").close()

class _Box:
    def __init__(self, **_kwargs): pass
    def set_border_width(self, *_args): pass
    def pack_start(self, *_args): pass

class _Label:
    def __init__(self, **_kwargs): pass
    def set_xalign(self, *_args): pass
    def set_line_wrap(self, *_args): pass

class _ProgressBar:
    def set_pulse_step(self, *_args): pass
    def pulse(self): return False

class Gtk:
    class WindowPosition: CENTER = 0
    class Orientation: VERTICAL = 0
    Window, Box, Label, ProgressBar = _Window, _Box, _Label, _ProgressBar
    running = False
    @classmethod
    def main(cls):
        cls.running = True
        while cls.running:
            fd, callback = GLib.watch
            ready, _, _ = select.select([fd], [], [], 0.02)
            if ready and not callback(fd, GLib.IO_HUP):
                break
    @classmethod
    def main_quit(cls): cls.running = False
""",
        encoding="utf-8",
    )
    return tmp_path


def _start_helper(
    tmp_path: Path, parent_read_fd: int, shown_marker: Path
) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_fake_gtk(tmp_path))
    environment["FAKE_GTK_SHOWN"] = str(shown_marker)
    return subprocess.Popen(
        (
            sys.executable,
            str(PROGRESS_HELPER),
            "PICASSO Test",
            "--parent-lifetime-fd",
            str(parent_read_fd),
        ),
        close_fds=True,
        env=environment,
        pass_fds=(parent_read_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for(path: Path, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def test_progress_helper_exits_only_when_its_own_parent_pipe_closes(
    tmp_path: Path,
) -> None:
    old_read, old_write = os.pipe()
    new_read, new_write = os.pipe()
    old_marker = tmp_path / "old-window-shown"
    new_marker = tmp_path / "new-window-shown"
    old_helper = _start_helper(tmp_path / "old", old_read, old_marker)
    new_helper = _start_helper(tmp_path / "new", new_read, new_marker)
    os.close(old_read)
    os.close(new_read)
    try:
        assert _wait_for(old_marker)
        assert _wait_for(new_marker)
        os.close(old_write)
        assert old_helper.wait(timeout=2) == 0
        assert new_helper.poll() is None
    finally:
        os.close(new_write)
        if old_helper.poll() is None:
            old_helper.kill()
            old_helper.wait()
        if new_helper.poll() is None:
            new_helper.wait(timeout=2)


def test_progress_helper_does_not_show_window_if_parent_died_before_start(
    tmp_path: Path,
) -> None:
    parent_read, parent_write = os.pipe()
    os.close(parent_write)
    marker = tmp_path / "window-shown"
    helper = _start_helper(tmp_path, parent_read, marker)
    os.close(parent_read)
    assert helper.wait(timeout=2) == 0
    assert not marker.exists()
