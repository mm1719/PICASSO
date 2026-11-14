from __future__ import annotations

import os
import stat
from pathlib import Path

from plankton_core import JobStore, write_outputs
from pytest import MonkeyPatch


def test_published_outputs_are_group_readable_by_the_usb_export_service(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")
    output_directory = tmp_path / "job-1" / "outputs"

    previous_umask = os.umask(0o007)
    try:
        write_outputs(store, "job-1", output_directory, "2026-08-04T00:00:00+00:00")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(output_directory.stat().st_mode) & 0o050 == 0o050
    assert all(
        stat.S_IMODE(path.stat().st_mode) & 0o040 == 0o040 for path in output_directory.iterdir()
    )


def test_output_publish_fsyncs_published_tree_and_parent(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")
    output_directory = tmp_path / "job-1" / "outputs"
    fsynced_directories: list[Path] = []
    opened_directories: dict[int, Path] = {}
    real_open = os.open
    real_fsync = os.fsync

    def track_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        if flags & os.O_DIRECTORY:
            opened_directories[descriptor] = Path(path).resolve()
        return descriptor

    def track_fsync(descriptor: int) -> None:
        if descriptor in opened_directories:
            fsynced_directories.append(opened_directories[descriptor])
        real_fsync(descriptor)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fsync", track_fsync)

    write_outputs(store, "job-1", output_directory, "2026-08-04T00:00:00+00:00")

    parent = output_directory.parent.resolve()
    assert parent in fsynced_directories
    assert any(path.parent == parent for path in fsynced_directories)
