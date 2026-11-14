from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from plankton_core import JobState, JobStore
from plankton_worker import Worker, WorkerSettings
from pytest import LogCaptureFixture


class RecordingUsb:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, operation: str, **_args: object) -> Any:
        self.calls.append(operation)
        return None


def test_job_scoped_usb_operations_emit_traceable_events(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    settings = WorkerSettings(
        database_path=tmp_path / "app.db",
        job_root=tmp_path / "jobs",
        gpu_lock_path=tmp_path / "run" / "gpu.lock",
    )
    store = JobStore(settings.database_path)
    store.initialize()
    store.create_requested_job(
        "job-1",
        "fake-model",
        "fake-hash",
        "fake-device",
        "science",
    )
    for state in (
        JobState.SCANNING,
        JobState.IMPORTING,
        JobState.VALIDATING,
        JobState.WAITING_FOR_GPU,
        JobState.INFERENCING,
        JobState.GENERATING_OUTPUTS,
        JobState.ARCHIVING,
        JobState.EXPORTING,
    ):
        store.transition("job-1", state)
    usb = RecordingUsb()
    caplog.set_level(logging.INFO)

    assert Worker(settings, usb).run_once()

    messages = [record.getMessage() for record in caplog.records]
    assert usb.calls == ["export_results", "release"]
    for operation in usb.calls:
        assert any(
            "job_id=job-1" in message
            and "state=" in message
            and operation in message
            for message in messages
        ), f"missing traceable event for {operation}"
