from __future__ import annotations

import csv
import json
import logging
import math
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from statistics import median
from typing import cast
from uuid import uuid4

from .inference_contract import (
    CLASS_TARGETS,
    LOW_CONFIDENCE_THRESHOLD,
    FakeInferenceAdapter,
    FakeUsbAdapter,
    Prediction,
    RankedTarget,
    rank_prediction,
)

LOGGER = logging.getLogger(__name__)


class JobState(StrEnum):
    CREATED = "CREATED"
    SCANNING = "SCANNING"
    IMPORTING = "IMPORTING"
    VALIDATING = "VALIDATING"
    WAITING_FOR_GPU = "WAITING_FOR_GPU"
    INFERENCING = "INFERENCING"
    GENERATING_OUTPUTS = "GENERATING_OUTPUTS"
    ARCHIVING = "ARCHIVING"
    EXPORTING = "EXPORTING"
    WAITING_FOR_USB = "WAITING_FOR_USB"
    COMPLETED = "COMPLETED"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"
    FAILED = "FAILED"


class RuntimeMode(StrEnum):
    INFERENCE_ENABLED = "inference_enabled"
    TRAINING = "training"


TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.SECURITY_BLOCKED, JobState.FAILED})
ALLOWED_TRANSITIONS = {
    JobState.CREATED: frozenset({JobState.SCANNING}),
    JobState.SCANNING: frozenset(
        {JobState.IMPORTING, JobState.SECURITY_BLOCKED, JobState.WAITING_FOR_USB}
    ),
    JobState.IMPORTING: frozenset(
        {JobState.VALIDATING, JobState.SECURITY_BLOCKED, JobState.WAITING_FOR_USB}
    ),
    JobState.VALIDATING: frozenset({JobState.WAITING_FOR_GPU}),
    JobState.WAITING_FOR_GPU: frozenset({JobState.INFERENCING}),
    JobState.INFERENCING: frozenset(
        {JobState.WAITING_FOR_GPU, JobState.GENERATING_OUTPUTS}
    ),
    JobState.GENERATING_OUTPUTS: frozenset({JobState.ARCHIVING}),
    JobState.ARCHIVING: frozenset({JobState.EXPORTING}),
    JobState.EXPORTING: frozenset({JobState.COMPLETED, JobState.WAITING_FOR_USB}),
    JobState.WAITING_FOR_USB: frozenset(
        {JobState.SCANNING, JobState.IMPORTING, JobState.EXPORTING}
    ),
}


@dataclass(frozen=True, slots=True)
class MetadataMatch:
    status: str
    values: dict[str, str]
    explicit_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    state: JobState
    started_at: str
    completed_at: str | None
    model_name: str
    checkpoint_sha256: str


@dataclass(frozen=True, slots=True)
class JobRequest:
    job_id: str
    device_id: str
    source_relative_path: str
    metadata_relative_path: str | None
    device_uuid: str = ""
    device_serial: str = ""
    safe_eject: bool = False


@dataclass(frozen=True, slots=True)
class JobProgress:
    total_count: int
    processed_count: int
    images_per_second: float
    eta_seconds: int | None
    stage_eta_seconds: int | None
    job_eta_seconds: int | None
    total_bytes: int = 0
    processed_bytes: int = 0
    throughput_bytes_per_second: float = 0.0
    progress_unit: str = "images"
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class JobFailure:
    code: str
    message: str
    relative_paths: tuple[str, ...]


@dataclass(slots=True)
class StoredPrediction:
    photo_id: str
    source_relative_path: str
    original_filename: str
    metadata_status: str
    metadata: dict[str, str]
    ranks: list[RankedTarget]


@dataclass(frozen=True, slots=True)
class StoredRejection:
    photo_id: str
    source_relative_path: str
    original_filename: str
    rejection_stage: str
    rejection_reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class FakeJobResult:
    job_id: str
    database_path: Path
    output_directory: Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _normalize_path(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix().removeprefix("./")


def _path_photo_id(relative_path: str) -> str | None:
    normalized = _normalize_path(relative_path)
    if not normalized or normalized == ".":
        return None
    return PurePosixPath(normalized).with_suffix("").as_posix()


def _first_value(row: Mapping[str, str], columns: Sequence[str]) -> str | None:
    for column in columns:
        value = row.get(column, "").strip()
        if value:
            return value
    return None


def join_metadata(
    image_paths: Sequence[str], metadata_rows: Sequence[Mapping[str, str]] | None
) -> dict[str, MetadataMatch]:
    if metadata_rows is None:
        return {path: MetadataMatch("not_provided", {}) for path in image_paths}

    rows = [dict(row) for row in metadata_rows]
    explicit_columns = ("image_id", "object_id", "photo_id")
    path_columns = ("source_relative_path", "relative_path", "image_path")
    filename_columns = ("original_filename", "filename", "file_name")
    path_index: dict[str, list[int]] = defaultdict(list)
    filename_index: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        relative_path = _first_value(row, path_columns)
        filename = _first_value(row, filename_columns)
        if relative_path:
            path_index[_normalize_path(relative_path)].append(index)
        if filename:
            filename_index[PurePosixPath(filename).name].append(index)

    filenames = Counter(PurePosixPath(path).name for path in image_paths)
    matches: dict[str, MetadataMatch] = {}
    for path in image_paths:
        normalized = _normalize_path(path)
        candidates = set(path_index.get(normalized, ()))
        if not candidates and filenames[PurePosixPath(normalized).name] == 1:
            candidates.update(filename_index.get(PurePosixPath(normalized).name, ()))

        if len(candidates) > 1:
            matches[path] = MetadataMatch("ambiguous", {})
        elif not candidates:
            matches[path] = MetadataMatch("unmatched", {})
        else:
            row = rows[candidates.pop()]
            matches[path] = MetadataMatch("matched", row, _first_value(row, explicit_columns))
    return matches


class JobStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    model_name TEXT NOT NULL,
                    checkpoint_sha256 TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job
                ON jobs((1))
                WHERE state NOT IN ('COMPLETED', 'SECURITY_BLOCKED', 'FAILED');
                CREATE TABLE IF NOT EXISTS images (
                    image_id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    source_relative_path TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    photo_id TEXT,
                    metadata_status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'PENDING',
                    UNIQUE(job_id, source_relative_path)
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    image_id INTEGER NOT NULL REFERENCES images(image_id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL,
                    class_index INTEGER NOT NULL,
                    target_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    softmax_score REAL NOT NULL,
                    PRIMARY KEY(image_id, rank)
                );
                CREATE TABLE IF NOT EXISTS rejections (
                    image_id INTEGER PRIMARY KEY REFERENCES images(image_id) ON DELETE CASCADE,
                    rejection_stage TEXT NOT NULL,
                    rejection_reason TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_requests (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                    device_id TEXT NOT NULL,
                    source_relative_path TEXT NOT NULL,
                    metadata_relative_path TEXT,
                    device_uuid TEXT NOT NULL DEFAULT '',
                    device_serial TEXT NOT NULL DEFAULT '',
                    safe_eject INTEGER NOT NULL DEFAULT 0 CHECK (safe_eject IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS job_progress (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                    total_count INTEGER NOT NULL DEFAULT 0,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    images_per_second REAL NOT NULL DEFAULT 0,
                    eta_seconds INTEGER,
                    stage_eta_seconds INTEGER,
                    job_eta_seconds INTEGER,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    processed_bytes INTEGER NOT NULL DEFAULT 0,
                    throughput_bytes_per_second REAL NOT NULL DEFAULT 0,
                    progress_unit TEXT NOT NULL DEFAULT 'images',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS eta_stage_profiles (
                    stage TEXT PRIMARY KEY,
                    setup_seconds REAL NOT NULL,
                    seconds_per_item REAL NOT NULL,
                    sample_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eta_stage_samples (
                    stage TEXT NOT NULL,
                    item_count INTEGER NOT NULL CHECK (item_count > 0),
                    elapsed_seconds REAL NOT NULL CHECK (elapsed_seconds > 0),
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_failures (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    relative_paths_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_waits (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                    resume_state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    mode TEXT NOT NULL
                );
                INSERT OR IGNORE INTO runtime_settings VALUES (1, 'inference_enabled');
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(job_requests)")
            }
            for name in ("device_uuid", "device_serial"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE job_requests ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            if "safe_eject" not in columns:
                connection.execute(
                    "ALTER TABLE job_requests ADD COLUMN safe_eject INTEGER NOT NULL DEFAULT 1"
                )
            progress_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(job_progress)")
            }
            for name in ("stage_eta_seconds", "job_eta_seconds"):
                if name not in progress_columns:
                    connection.execute(f"ALTER TABLE job_progress ADD COLUMN {name} INTEGER")
            for name, definition in (
                ("total_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("processed_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("throughput_bytes_per_second", "REAL NOT NULL DEFAULT 0"),
                ("progress_unit", "TEXT NOT NULL DEFAULT 'images'"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in progress_columns:
                    connection.execute(f"ALTER TABLE job_progress ADD COLUMN {name} {definition}")
            connection.execute(
                "UPDATE job_progress SET updated_at = ? WHERE updated_at = ''",
                (_now(),),
            )
            profile_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(eta_stage_profiles)")
            }
            if profile_columns and "setup_seconds" not in profile_columns:
                # Legacy equal-per-job EWMA values cannot be converted safely to a fixed-cost model.
                connection.execute("DROP TABLE eta_stage_profiles")
                connection.execute(
                    """
                    CREATE TABLE eta_stage_profiles (
                        stage TEXT PRIMARY KEY,
                        setup_seconds REAL NOT NULL,
                        seconds_per_item REAL NOT NULL,
                        sample_count INTEGER NOT NULL
                    )
                    """
                )

    def create_requested_job(
        self,
        job_id: str,
        model_name: str,
        checkpoint_sha256: str,
        device_id: str,
        source_relative_path: str,
        metadata_relative_path: str | None = None,
        *,
        device_uuid: str = "",
        device_serial: str = "",
        safe_eject: bool = False,
    ) -> None:
        try:
            with self._connection() as connection:
                created_at = _now()
                connection.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, NULL, ?, ?)",
                    (job_id, JobState.CREATED, created_at, model_name, checkpoint_sha256),
                )
                connection.execute(
                    """
                    INSERT INTO job_requests(
                        job_id, device_id, source_relative_path, metadata_relative_path,
                        device_uuid, device_serial, safe_eject
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        device_id,
                        source_relative_path,
                        metadata_relative_path,
                        device_uuid,
                        device_serial,
                        int(safe_eject),
                    ),
                )
                connection.execute(
                    "INSERT INTO job_progress(job_id, updated_at) VALUES (?, ?)",
                    (job_id, created_at),
                )
        except sqlite3.IntegrityError as error:
            raise RuntimeError("an active inference job already exists") from error
        LOGGER.info(
            "job_event job_id=%s state=%s event_code=job_created message=job_created",
            job_id,
            JobState.CREATED,
        )

    def request(self, job_id: str) -> JobRequest:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM job_requests WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobRequest(
            job_id=str(row["job_id"]),
            device_id=str(row["device_id"]),
            source_relative_path=str(row["source_relative_path"]),
            metadata_relative_path=(
                str(row["metadata_relative_path"]) if row["metadata_relative_path"] else None
            ),
            device_uuid=str(row["device_uuid"]),
            device_serial=str(row["device_serial"]),
            safe_eject=bool(row["safe_eject"]),
        )

    def claim_created_job(self) -> JobRequest | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE jobs SET state = 'SCANNING'
                WHERE job_id = (
                    SELECT job_id FROM jobs WHERE state = 'CREATED' ORDER BY started_at LIMIT 1
                ) AND state = 'CREATED'
                RETURNING job_id
                """
            ).fetchone()
        return self.request(str(row["job_id"])) if row is not None else None

    def waiting_job(self, state: JobState) -> JobRequest | None:
        if state not in {JobState.WAITING_FOR_GPU, JobState.WAITING_FOR_USB}:
            raise ValueError("state is not resumable")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_id FROM jobs WHERE state = ? ORDER BY started_at LIMIT 1", (state,)
            ).fetchone()
        return self.request(str(row["job_id"])) if row is not None else None

    def active_request(self) -> JobRequest | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE state NOT IN ('COMPLETED', 'SECURITY_BLOCKED', 'FAILED')
                ORDER BY started_at LIMIT 1
                """
            ).fetchone()
        return self.request(str(row["job_id"])) if row is not None else None

    def wait_for_usb(self, job_id: str) -> None:
        current = self.job(job_id).state
        if current not in {JobState.SCANNING, JobState.IMPORTING, JobState.EXPORTING}:
            raise ValueError(f"Job cannot wait for USB from {current}")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO job_waits VALUES (?, ?)
                ON CONFLICT(job_id) DO UPDATE SET resume_state = excluded.resume_state
                """,
                (job_id, current),
            )
            connection.execute(
                "UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.WAITING_FOR_USB, job_id)
            )
            connection.execute(
                """
                UPDATE job_progress SET images_per_second = 0, eta_seconds = NULL,
                    stage_eta_seconds = NULL, job_eta_seconds = NULL WHERE job_id = ?
                """,
                (job_id,),
            )
        LOGGER.info(
            "job_event job_id=%s state=%s event_code=device_unavailable message=resume_%s",
            job_id,
            JobState.WAITING_FOR_USB,
            current,
        )

    def resume_waiting_for_usb(self, job_id: str) -> JobState:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT resume_state FROM job_waits
                WHERE job_id = ? AND EXISTS (
                    SELECT 1 FROM jobs WHERE job_id = ? AND state = 'WAITING_FOR_USB'
                )
                """,
                (job_id, job_id),
            ).fetchone()
            if row is None:
                raise ValueError("waiting job has no resume state")
            resume_state = JobState(row["resume_state"])
            connection.execute(
                "UPDATE jobs SET state = ? WHERE job_id = ?", (resume_state, job_id)
            )
            connection.execute("DELETE FROM job_waits WHERE job_id = ?", (job_id,))
        return resume_state

    def progress(self, job_id: str) -> JobProgress:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM job_progress WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return JobProgress(0, 0, 0.0, None, None, None)
        return JobProgress(
            total_count=int(row["total_count"]),
            processed_count=int(row["processed_count"]),
            images_per_second=float(row["images_per_second"]),
            eta_seconds=int(row["eta_seconds"]) if row["eta_seconds"] is not None else None,
            stage_eta_seconds=(
                int(row["stage_eta_seconds"])
                if row["stage_eta_seconds"] is not None
                else None
            ),
            job_eta_seconds=(
                int(row["job_eta_seconds"])
                if row["job_eta_seconds"] is not None
                else None
            ),
            total_bytes=int(row["total_bytes"] or 0),
            processed_bytes=int(row["processed_bytes"] or 0),
            throughput_bytes_per_second=float(row["throughput_bytes_per_second"] or 0),
            progress_unit=str(row["progress_unit"] or "images"),
            updated_at=str(row["updated_at"] or ""),
        )

    def record_eta_stage_timing(
        self,
        stage: str,
        *,
        item_count: int,
        elapsed_seconds: float,
    ) -> None:
        if item_count <= 0 or elapsed_seconds <= 0:
            return
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO eta_stage_samples(stage, item_count, elapsed_seconds, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (stage, item_count, elapsed_seconds, datetime.now(UTC).isoformat()),
            )
            self._recalculate_eta_stage_profile(connection, stage)

    @staticmethod
    def _recalculate_eta_stage_profile(connection: sqlite3.Connection, stage: str) -> None:
        rows = connection.execute(
            """
            SELECT item_count, elapsed_seconds
            FROM eta_stage_samples
            WHERE stage = ?
            ORDER BY item_count, recorded_at
            """,
            (stage,),
        ).fetchall()
        by_count: dict[int, list[float]] = defaultdict(list)
        for row in rows:
            by_count[int(row["item_count"])].append(float(row["elapsed_seconds"]))
        if len(rows) < 3 or len(by_count) < 2:
            connection.execute("DELETE FROM eta_stage_profiles WHERE stage = ?", (stage,))
            return
        points = [(count, float(median(elapsed))) for count, elapsed in sorted(by_count.items())]
        slopes = [
            (later_elapsed - earlier_elapsed) / (later_count - earlier_count)
            for index, (earlier_count, earlier_elapsed) in enumerate(points)
            for later_count, later_elapsed in points[index + 1 :]
            if later_elapsed > earlier_elapsed
        ]
        if not slopes:
            connection.execute("DELETE FROM eta_stage_profiles WHERE stage = ?", (stage,))
            return
        seconds_per_item = float(median(slopes))
        setup_seconds = max(
            0.0,
            float(median([elapsed - seconds_per_item * count for count, elapsed in points])),
        )
        connection.execute(
            """
            INSERT INTO eta_stage_profiles(stage, setup_seconds, seconds_per_item, sample_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(stage) DO UPDATE SET
                setup_seconds = excluded.setup_seconds,
                seconds_per_item = excluded.seconds_per_item,
                sample_count = excluded.sample_count
            """,
            (stage, setup_seconds, seconds_per_item, len(rows)),
        )

    def estimated_stage_seconds(self, stage: str, item_count: int) -> int | None:
        if item_count <= 0:
            return 0
        with self._connection() as connection:
            row = connection.execute(
                "SELECT setup_seconds, seconds_per_item FROM eta_stage_profiles WHERE stage = ?",
                (stage,),
            ).fetchone()
        if row is None:
            return None
        return max(
            1,
            math.ceil(float(row["setup_seconds"]) + float(row["seconds_per_item"]) * item_count),
        )

    def set_failure(
        self,
        job_id: str,
        code: str,
        message: str,
        relative_paths: Sequence[str] = (),
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO job_failures VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    code = excluded.code,
                    message = excluded.message,
                    relative_paths_json = excluded.relative_paths_json
                """,
                (job_id, code, message, json.dumps(list(relative_paths), ensure_ascii=False)),
            )

    def failure(self, job_id: str) -> JobFailure | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT code, message, relative_paths_json FROM job_failures WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        paths = cast(list[str], json.loads(str(row["relative_paths_json"])))
        return JobFailure(str(row["code"]), str(row["message"]), tuple(paths))

    def set_progress(
        self,
        job_id: str,
        *,
        total_count: int,
        processed_count: int,
        images_per_second: float = 0,
        eta_seconds: int | None = None,
        stage_eta_seconds: int | None = None,
        job_eta_seconds: int | None = None,
        total_bytes: int = 0,
        processed_bytes: int = 0,
        throughput_bytes_per_second: float = 0,
        progress_unit: str = "images",
    ) -> None:
        if not 0 <= processed_count <= total_count:
            raise ValueError("processed count must be between zero and total count")
        if total_bytes < 0 or not 0 <= processed_bytes <= total_bytes:
            raise ValueError("processed bytes must be between zero and total bytes")
        with self._connection() as connection:
            updated_at = _now()
            previous = connection.execute(
                "SELECT updated_at FROM job_progress WHERE job_id = ?", (job_id,)
            ).fetchone()
            if previous is not None and previous["updated_at"]:
                try:
                    previous_at = datetime.fromisoformat(str(previous["updated_at"]))
                    current_at = datetime.fromisoformat(updated_at)
                    if current_at <= previous_at:
                        updated_at = (previous_at + timedelta(microseconds=1)).isoformat()
                except ValueError:
                    pass
            connection.execute(
                """
                INSERT INTO job_progress(
                    job_id, total_count, processed_count, images_per_second, eta_seconds,
                    stage_eta_seconds, job_eta_seconds, total_bytes, processed_bytes,
                    throughput_bytes_per_second, progress_unit, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    total_count = excluded.total_count,
                    processed_count = excluded.processed_count,
                    images_per_second = excluded.images_per_second,
                    eta_seconds = excluded.eta_seconds,
                    stage_eta_seconds = excluded.stage_eta_seconds,
                    job_eta_seconds = excluded.job_eta_seconds,
                    total_bytes = excluded.total_bytes,
                    processed_bytes = excluded.processed_bytes,
                    throughput_bytes_per_second = excluded.throughput_bytes_per_second,
                    progress_unit = excluded.progress_unit,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    total_count,
                    processed_count,
                    images_per_second,
                    eta_seconds,
                    stage_eta_seconds,
                    job_eta_seconds,
                    total_bytes,
                    processed_bytes,
                    throughput_bytes_per_second,
                    progress_unit,
                    updated_at,
                ),
            )

    def runtime_mode(self) -> RuntimeMode:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT mode FROM runtime_settings WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("runtime settings are not initialized")
        return RuntimeMode(row["mode"])

    def set_runtime_mode(self, mode: RuntimeMode, *, require_idle: bool = False) -> None:
        with self._connection() as connection:
            if require_idle:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    """
                    SELECT 1 FROM jobs
                    WHERE state NOT IN ('COMPLETED', 'SECURITY_BLOCKED', 'FAILED') LIMIT 1
                    """
                ).fetchone() is not None:
                    raise RuntimeError("cannot pause while an active inference job exists")
            connection.execute("UPDATE runtime_settings SET mode = ? WHERE singleton = 1", (mode,))

    def create_job(
        self,
        job_id: str,
        model_name: str,
        checkpoint_sha256: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, NULL, ?, ?)",
                (job_id, JobState.CREATED, _now(), model_name, checkpoint_sha256),
            )

    def job(self, job_id: str) -> JobRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobRecord(
            job_id=str(row["job_id"]),
            state=JobState(row["state"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]) if row["completed_at"] else None,
            model_name=str(row["model_name"]),
            checkpoint_sha256=str(row["checkpoint_sha256"]),
        )

    def transition(self, job_id: str, target: JobState, completed_at: str | None = None) -> None:
        current = self.job(job_id).state
        if current == target:
            return
        if current in TERMINAL_STATES:
            raise ValueError(f"Terminal job cannot transition from {current} to {target}")
        if target != JobState.FAILED and target not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"Invalid job transition from {current} to {target}")
        resolved_completed_at = completed_at or _now() if target in TERMINAL_STATES else None
        with self._connection() as connection:
            connection.execute(
                "UPDATE jobs SET state = ?, completed_at = ? WHERE job_id = ?",
                (target, resolved_completed_at, job_id),
            )
        LOGGER.info(
            "job_event job_id=%s state=%s event_code=state_transition message=from_%s",
            job_id,
            target,
            current,
        )

    def add_image(
        self,
        job_id: str,
        source_relative_path: str,
        photo_id: str | None,
        metadata: MetadataMatch,
    ) -> None:
        self.add_images(job_id, ((source_relative_path, photo_id, metadata),))

    def add_images(
        self,
        job_id: str,
        images: Sequence[tuple[str, str | None, MetadataMatch]],
    ) -> None:
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO images (
                    job_id, source_relative_path, original_filename, photo_id,
                    metadata_status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        job_id,
                        source_relative_path,
                        PurePosixPath(source_relative_path).name,
                        photo_id,
                        metadata.status,
                        json.dumps(metadata.values, ensure_ascii=False, sort_keys=True),
                    )
                    for source_relative_path, photo_id, metadata in images
                ),
            )

    def _image_id(self, connection: sqlite3.Connection, job_id: str, path: str) -> int:
        row = connection.execute(
            "SELECT image_id FROM images WHERE job_id = ? AND source_relative_path = ?",
            (job_id, path),
        ).fetchone()
        if row is None:
            raise KeyError(path)
        return int(row["image_id"])

    def _record_prediction(
        self, connection: sqlite3.Connection, job_id: str, prediction: Prediction
    ) -> None:
        ranks = prediction.low_confidence_top3 or (prediction.top1,)
        image_id = self._image_id(connection, job_id, prediction.source_relative_path)
        connection.execute("DELETE FROM rejections WHERE image_id = ?", (image_id,))
        connection.execute("DELETE FROM predictions WHERE image_id = ?", (image_id,))
        connection.executemany(
            "INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    image_id,
                    rank.rank,
                    rank.class_index,
                    rank.target_id,
                    rank.display_name,
                    rank.softmax_score,
                )
                for rank in ranks
            ],
        )
        connection.execute(
            "UPDATE images SET outcome = 'PREDICTED' WHERE image_id = ?", (image_id,)
        )

    def record_prediction(self, job_id: str, prediction: Prediction) -> None:
        with self._connection() as connection:
            self._record_prediction(connection, job_id, prediction)

    def checkpoint_predictions(
        self,
        job_id: str,
        predictions: Sequence[Prediction],
        *,
        total_count: int,
        processed_count: int,
        images_per_second: float,
        eta_seconds: int | None,
        stage_eta_seconds: int | None = None,
        job_eta_seconds: int | None = None,
    ) -> None:
        if not 0 <= processed_count <= total_count:
            raise ValueError("processed count must be between zero and total count")
        with self._connection() as connection:
            for prediction in predictions:
                self._record_prediction(connection, job_id, prediction)
            connection.execute(
                """
                UPDATE job_progress SET total_count = ?, processed_count = ?,
                    images_per_second = ?, eta_seconds = ?, stage_eta_seconds = ?,
                    job_eta_seconds = ? WHERE job_id = ?
                """,
                (
                    total_count,
                    processed_count,
                    images_per_second,
                    eta_seconds,
                    stage_eta_seconds,
                    job_eta_seconds,
                    job_id,
                ),
            )

    def clear_validation_results(self, job_id: str) -> None:
        if self.job(job_id).state != JobState.VALIDATING:
            raise ValueError("validation results can only be reset while validating")
        with self._connection() as connection:
            connection.execute("DELETE FROM images WHERE job_id = ?", (job_id,))
            connection.execute(
                """
                UPDATE job_progress SET total_count = 0, processed_count = 0,
                    images_per_second = 0, eta_seconds = NULL, stage_eta_seconds = NULL,
                    job_eta_seconds = NULL WHERE job_id = ?
                """,
                (job_id,),
            )

    def pending_image_paths(self, job_id: str) -> tuple[str, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT source_relative_path FROM images
                WHERE job_id = ? AND outcome = 'PENDING'
                ORDER BY source_relative_path
                """,
                (job_id,),
            ).fetchall()
        return tuple(str(row["source_relative_path"]) for row in rows)

    def record_rejection(
        self,
        job_id: str,
        source_relative_path: str,
        stage: str,
        reason: str,
        detail: str,
    ) -> None:
        self.record_rejections(job_id, ((source_relative_path, stage, reason, detail),))

    def record_rejections(
        self,
        job_id: str,
        rejections: Sequence[tuple[str, str, str, str]],
    ) -> None:
        if not rejections:
            return
        with self._connection() as connection:
            rows = [
                (self._image_id(connection, job_id, path), stage, reason, detail)
                for path, stage, reason, detail in rejections
            ]
            connection.executemany(
                "DELETE FROM predictions WHERE image_id = ?",
                ((image_id,) for image_id, _, _, _ in rows),
            )
            connection.executemany(
                """
                INSERT INTO rejections VALUES (?, ?, ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                    rejection_stage = excluded.rejection_stage,
                    rejection_reason = excluded.rejection_reason,
                    detail = excluded.detail
                """,
                rows,
            )
            connection.executemany(
                "UPDATE images SET outcome = 'REJECTED' WHERE image_id = ?",
                ((image_id,) for image_id, _, _, _ in rows),
            )

    def predictions(self, job_id: str) -> tuple[StoredPrediction, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT i.photo_id, i.source_relative_path, i.original_filename,
                       i.metadata_status, i.metadata_json,
                       p.rank, p.class_index, p.target_id, p.display_name, p.softmax_score
                FROM images i JOIN predictions p ON p.image_id = i.image_id
                WHERE i.job_id = ? AND i.outcome = 'PREDICTED'
                ORDER BY i.source_relative_path, p.rank
                """,
                (job_id,),
            ).fetchall()
        grouped: dict[str, StoredPrediction] = {}
        for row in rows:
            path = str(row["source_relative_path"])
            if path not in grouped:
                metadata = cast(dict[str, str], json.loads(str(row["metadata_json"])))
                grouped[path] = StoredPrediction(
                    photo_id=str(row["photo_id"] or ""),
                    source_relative_path=path,
                    original_filename=str(row["original_filename"]),
                    metadata_status=str(row["metadata_status"]),
                    metadata=metadata,
                    ranks=[],
                )
            grouped[path].ranks.append(
                RankedTarget(
                    rank=int(row["rank"]),
                    class_index=int(row["class_index"]),
                    target_id=str(row["target_id"]),
                    display_name=str(row["display_name"]),
                    softmax_score=float(row["softmax_score"]),
                )
            )
        return tuple(grouped.values())

    def rejections(self, job_id: str) -> tuple[StoredRejection, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT i.photo_id, i.source_relative_path, i.original_filename,
                       r.rejection_stage, r.rejection_reason, r.detail
                FROM images i JOIN rejections r ON r.image_id = i.image_id
                WHERE i.job_id = ? AND i.outcome = 'REJECTED'
                ORDER BY i.source_relative_path
                """,
                (job_id,),
            ).fetchall()
        return tuple(
            StoredRejection(
                photo_id=str(row["photo_id"] or ""),
                source_relative_path=str(row["source_relative_path"]),
                original_filename=str(row["original_filename"]),
                rejection_stage=str(row["rejection_stage"]),
                rejection_reason=str(row["rejection_reason"]),
                detail=str(row["detail"]),
            )
            for row in rows
        )

    def counts(self, job_id: str) -> dict[str, int]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS discovered,
                       SUM(outcome = 'PREDICTED') AS predicted,
                       SUM(outcome = 'REJECTED') AS rejected
                FROM images WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            low = connection.execute(
                """
                SELECT COUNT(*) FROM images i JOIN predictions p ON p.image_id = i.image_id
                WHERE i.job_id = ? AND p.rank = 1 AND p.softmax_score < ?
                """,
                (job_id, LOW_CONFIDENCE_THRESHOLD),
            ).fetchone()
        if row is None or low is None:
            raise KeyError(job_id)
        return {
            "discovered": int(row["discovered"] or 0),
            "predicted": int(row["predicted"] or 0),
            "low_confidence": int(low[0]),
            "rejected": int(row["rejected"] or 0),
        }


def write_outputs(
    store: JobStore,
    job_id: str,
    output_directory: Path,
    completed_at: str,
    *,
    usb_ejected: bool = False,
) -> None:
    if output_directory.exists():
        raise FileExistsError(output_directory)
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    predictions = store.predictions(job_id)
    rejections = store.rejections(job_id)
    job = store.job(job_id)
    metadata_columns = sorted({column for item in predictions for column in item.metadata})

    with tempfile.TemporaryDirectory(dir=output_directory.parent, prefix="outputs-") as temporary:
        temporary_path = Path(temporary)
        with (temporary_path / "predictions_top1.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            fields = [
                "photo_id",
                "source_relative_path",
                "original_filename",
                "target_id",
                "target_display_name",
                "softmax_score",
                "metadata_status",
                *(f"metadata__{column}" for column in metadata_columns),
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in predictions:
                top1 = item.ranks[0]
                row: dict[str, str] = {
                    "photo_id": item.photo_id,
                    "source_relative_path": item.source_relative_path,
                    "original_filename": item.original_filename,
                    "target_id": top1.target_id,
                    "target_display_name": top1.display_name,
                    "softmax_score": f"{top1.softmax_score:.6f}",
                    "metadata_status": item.metadata_status,
                }
                row.update({f"metadata__{key}": value for key, value in item.metadata.items()})
                writer.writerow(row)

        with (temporary_path / "low_confidence_top3.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            fields = ["photo_id", "source_relative_path", "original_filename"]
            for rank_number in range(1, 4):
                fields.extend(
                    [
                        f"rank_{rank_number}_target_id",
                        f"rank_{rank_number}_display_name",
                        f"rank_{rank_number}_softmax_score",
                    ]
                )
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in predictions:
                if item.ranks[0].softmax_score >= LOW_CONFIDENCE_THRESHOLD:
                    continue
                row = {
                    "photo_id": item.photo_id,
                    "source_relative_path": item.source_relative_path,
                    "original_filename": item.original_filename,
                }
                for ranked in item.ranks:
                    row[f"rank_{ranked.rank}_target_id"] = ranked.target_id
                    row[f"rank_{ranked.rank}_display_name"] = ranked.display_name
                    row[f"rank_{ranked.rank}_softmax_score"] = f"{ranked.softmax_score:.6f}"
                writer.writerow(row)

        with (temporary_path / "rejected_images.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "photo_id",
                    "source_relative_path",
                    "original_filename",
                    "rejection_stage",
                    "rejection_reason",
                    "detail",
                ],
            )
            writer.writeheader()
            writer.writerows(
                {
                    "photo_id": item.photo_id,
                    "source_relative_path": item.source_relative_path,
                    "original_filename": item.original_filename,
                    "rejection_stage": item.rejection_stage,
                    "rejection_reason": item.rejection_reason,
                    "detail": item.detail,
                }
                for item in rejections
            )

        counts = store.counts(job_id)
        totals = Counter(item.ranks[0].target_id for item in predictions)
        low_counts = Counter(
            item.ranks[0].target_id for item in predictions
            if item.ranks[0].softmax_score < LOW_CONFIDENCE_THRESHOLD
        )
        aliases = {
            "370437": "Heterokontophyta (Diatom)",
            "450030": "Myzozoa (Dinoflagellate)",
        }
        with (temporary_path / "counts_summary.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer_counts = csv.writer(handle)
            writer_counts.writerow(["name", "target_id", "total_count", "low_confidence_count"])
            writer_counts.writerow(["All images", "", counts["discovered"], ""])
            writer_counts.writerow(["Predicted images", "", counts["predicted"], counts["low_confidence"]])
            writer_counts.writerow(["Rejected images", "", counts["rejected"], ""])
            for target_id, display_name in CLASS_TARGETS:
                writer_counts.writerow([
                    aliases.get(target_id, display_name), target_id, totals[target_id], low_counts[target_id]
                ])

        summary = {
            "schema_version": "1.0",
            "job_id": job.job_id,
            "status": JobState.COMPLETED,
            "started_at": job.started_at,
            "completed_at": completed_at,
            "model_name": job.model_name,
            "checkpoint_sha256": job.checkpoint_sha256,
            "score_type": "uncalibrated_softmax",
            "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
            "counts": counts,
            "usb_ejected": usb_ejected,
        }
        (temporary_path / "job_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for path in temporary_path.iterdir():
            os.chmod(path, 0o640)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o2750)
        _fsync_directory(temporary_path)
        temporary_path.rename(output_directory)
        _fsync_directory(output_directory.parent)


def register_source_images(
    store: JobStore,
    job_id: str,
    source_root: Path,
    metadata_rows: Sequence[Mapping[str, str]] | None = None,
    batch_size: int = 1000,
) -> tuple[tuple[str, ...], frozenset[str]]:
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    paths = FakeUsbAdapter(source_root).image_paths()
    metadata_matches = join_metadata(paths, metadata_rows)
    photo_ids = {path: metadata_matches[path].explicit_id or _path_photo_id(path) for path in paths}
    photo_id_counts = Counter(photo_id for photo_id in photo_ids.values() if photo_id)
    rejected_paths: set[str] = set()
    registrations: list[tuple[str, str, MetadataMatch]] = []
    for path in paths:
        photo_id = photo_ids[path]
        if photo_id is None:
            raise RuntimeError("safe source path did not produce a Photo ID")
        registrations.append((path, photo_id, metadata_matches[path]))
        if photo_id_counts[photo_id] > 1:
            rejected_paths.add(path)
    for offset in range(0, len(registrations), batch_size):
        store.add_images(job_id, registrations[offset : offset + batch_size])
    duplicate_rejections = [
        (
            path,
            "validation",
            "duplicate_photo_id",
            "Photo ID is duplicated within the job namespace.",
        )
        for path in sorted(rejected_paths)
    ]
    for offset in range(0, len(duplicate_rejections), batch_size):
        store.record_rejections(
            job_id,
            duplicate_rejections[offset : offset + batch_size],
        )
    return paths, frozenset(rejected_paths)


def run_fake_job_with_outputs(
    source_root: Path,
    scores_by_path: Mapping[str, Sequence[float]],
    work_directory: Path,
    metadata_rows: Sequence[Mapping[str, str]] | None = None,
    job_id: str | None = None,
    checkpoint_sha256: str = "fake-checkpoint",
) -> FakeJobResult:
    resolved_job_id = job_id or str(uuid4())
    database_path = work_directory / "app.db"
    output_directory = work_directory / "outputs"
    store = JobStore(database_path)
    store.initialize()
    store.create_job(
        resolved_job_id,
        "fake-model",
        checkpoint_sha256,
    )
    store.transition(resolved_job_id, JobState.SCANNING)
    store.transition(resolved_job_id, JobState.IMPORTING)
    store.transition(resolved_job_id, JobState.VALIDATING)
    paths, rejected_paths = register_source_images(
        store, resolved_job_id, source_root, metadata_rows
    )

    store.transition(resolved_job_id, JobState.WAITING_FOR_GPU)
    store.transition(resolved_job_id, JobState.INFERENCING)
    model = FakeInferenceAdapter(scores_by_path)
    for path in paths:
        if path not in rejected_paths:
            store.record_prediction(resolved_job_id, rank_prediction(path, model.scores_for(path)))

    store.transition(resolved_job_id, JobState.GENERATING_OUTPUTS)
    # ponytail: archive/export are no-ops in this fake slice; Stage 3/5 adapters replace them.
    store.transition(resolved_job_id, JobState.ARCHIVING)
    store.transition(resolved_job_id, JobState.EXPORTING)
    completed_at = _now()
    write_outputs(store, resolved_job_id, output_directory, completed_at)
    store.transition(resolved_job_id, JobState.COMPLETED, completed_at)
    return FakeJobResult(resolved_job_id, database_path, output_directory)
