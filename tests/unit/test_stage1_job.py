import csv
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from plankton_core import (
    CLASS_TARGETS,
    JobState,
    JobStore,
    MetadataMatch,
    join_metadata,
    rank_prediction,
    register_source_images,
    run_fake_job_with_outputs,
)


def scores(*values: tuple[int, float]) -> list[float]:
    result = [0.0] * len(CLASS_TARGETS)
    for class_index, score in values:
        result[class_index] = score
    return result


def test_job_state_survives_store_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    store = JobStore(database_path)
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")
    store.transition("job-1", JobState.SCANNING)

    reopened = JobStore(database_path)

    assert reopened.job("job-1").state == JobState.SCANNING
    reopened.transition("job-1", JobState.IMPORTING)
    assert JobStore(database_path).job("job-1").state == JobState.IMPORTING


def test_job_request_identity_columns_migrate_without_losing_existing_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "app.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                model_name TEXT NOT NULL,
                checkpoint_sha256 TEXT NOT NULL
            );
            CREATE TABLE job_requests (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                device_id TEXT NOT NULL,
                source_relative_path TEXT NOT NULL,
                metadata_relative_path TEXT
            );
            INSERT INTO jobs VALUES (
                'old-job', 'COMPLETED', '2026-08-05T00:00:00+00:00',
                '2026-08-05T00:01:00+00:00', 'fake-model', 'fake-hash'
            );
            INSERT INTO job_requests VALUES ('old-job', 'legacy-device', 'science', NULL);
            """
        )

    store = JobStore(database_path)
    store.initialize()
    assert store.request("old-job").device_uuid == ""
    assert store.request("old-job").safe_eject is True
    store.create_requested_job(
        "new-job",
        "fake-model",
        "fake-hash",
        "device-1",
        "science",
        device_uuid="FS-UUID-001",
        device_serial="SERIAL-001",
    )

    request = store.request("new-job")
    assert (request.device_uuid, request.device_serial) == (
        "FS-UUID-001",
        "SERIAL-001",
    )
    assert request.safe_eject is False


def test_job_progress_columns_migrate_and_persist(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                model_name TEXT NOT NULL,
                checkpoint_sha256 TEXT NOT NULL
            );
            CREATE TABLE job_progress (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                total_count INTEGER NOT NULL DEFAULT 0,
                processed_count INTEGER NOT NULL DEFAULT 0,
                images_per_second REAL NOT NULL DEFAULT 0,
                eta_seconds INTEGER
            );
            INSERT INTO jobs VALUES (
                'old-job', 'COMPLETED', '2026-08-05T00:00:00+00:00',
                '2026-08-05T00:01:00+00:00', 'fake-model', 'fake-hash'
            );
            INSERT INTO job_progress VALUES ('old-job', 10, 5, 2.0, 3);
            """
        )

    store = JobStore(database_path)
    store.initialize()
    migrated = store.progress("old-job")
    assert (migrated.stage_eta_seconds, migrated.job_eta_seconds) == (None, None)
    assert datetime.fromisoformat(migrated.updated_at).tzinfo == UTC
    assert (
        migrated.total_count,
        migrated.processed_count,
        migrated.images_per_second,
        migrated.eta_seconds,
        migrated.progress_unit,
        store.job("old-job").state,
    ) == (10, 5, 2.0, 3, "images", JobState.COMPLETED)

    store.initialize()
    assert store.progress("old-job") == migrated

    store.set_progress(
        "old-job",
        total_count=10,
        processed_count=6,
        images_per_second=2,
        eta_seconds=2,
        stage_eta_seconds=2,
        job_eta_seconds=7,
    )
    updated = store.progress("old-job")
    assert (updated.stage_eta_seconds, updated.job_eta_seconds) == (2, 7)


def test_progress_timestamp_strictly_advances_when_clock_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_now = "2026-08-29T04:00:00+00:00"
    monkeypatch.setattr("plankton_core.jobs._now", lambda: fixed_now)
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_requested_job(
        "job-1",
        "fake-model",
        "fake-hash",
        "device-1",
        "science",
        device_uuid="FS-UUID-001",
    )

    created_at = datetime.fromisoformat(store.progress("job-1").updated_at)
    store.set_progress("job-1", total_count=10, processed_count=1)
    first_event = datetime.fromisoformat(store.progress("job-1").updated_at)
    store.set_progress("job-1", total_count=10, processed_count=1)
    second_event = datetime.fromisoformat(store.progress("job-1").updated_at)

    assert created_at < first_event < second_event


def test_eta_stage_profile_requires_size_span_and_models_setup_cost(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    assert store.estimated_stage_seconds("INFERENCING", 100) is None

    store.record_eta_stage_timing("INFERENCING", item_count=1, elapsed_seconds=10.1)
    store.record_eta_stage_timing("INFERENCING", item_count=10, elapsed_seconds=11)
    assert store.estimated_stage_seconds("INFERENCING", 100) is None

    store.record_eta_stage_timing("INFERENCING", item_count=100, elapsed_seconds=20)
    assert store.estimated_stage_seconds("INFERENCING", 100) == 20
    assert store.estimated_stage_seconds("INFERENCING", 0) == 0


def test_eta_stage_profile_ignores_invalid_samples_and_supports_two_sizes(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()

    store.record_eta_stage_timing("IMPORTING", item_count=0, elapsed_seconds=1)
    store.record_eta_stage_timing("IMPORTING", item_count=1, elapsed_seconds=0)
    store.record_eta_stage_timing("IMPORTING", item_count=1, elapsed_seconds=1)
    store.record_eta_stage_timing("IMPORTING", item_count=1, elapsed_seconds=1)
    assert store.estimated_stage_seconds("IMPORTING", 2) is None

    store.record_eta_stage_timing("IMPORTING", item_count=2, elapsed_seconds=2)
    assert store.estimated_stage_seconds("IMPORTING", 1) == 1
    assert store.estimated_stage_seconds("IMPORTING", 2) == 2


def test_eta_stage_profile_uses_all_pairwise_slopes(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    for item_count, elapsed_seconds in ((1, 1), (2, 100), (3, 101), (100, 102)):
        store.record_eta_stage_timing(
            "VALIDATING", item_count=item_count, elapsed_seconds=elapsed_seconds
        )

    assert tuple(store.estimated_stage_seconds("VALIDATING", count) for count in (1, 100)) == (
        51,
        151,
    )


def test_eta_stage_profile_requires_elapsed_time_to_increase_with_size(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    for item_count in (1, 1, 2):
        store.record_eta_stage_timing(
            "FINALIZING", item_count=item_count, elapsed_seconds=10
        )

    assert store.estimated_stage_seconds("FINALIZING", 100) is None


def test_calibrated_eta_stage_profile_survives_store_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    store = JobStore(database_path)
    store.initialize()
    for item_count, elapsed_seconds in ((1, 1), (2, 2), (3, 3)):
        store.record_eta_stage_timing(
            "INFERENCING", item_count=item_count, elapsed_seconds=elapsed_seconds
        )
    assert store.estimated_stage_seconds("INFERENCING", 100) == 100

    reopened = JobStore(database_path)
    reopened.initialize()
    assert reopened.estimated_stage_seconds("INFERENCING", 100) == 100


def test_legacy_eta_profile_is_deleted_during_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "app.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE eta_stage_profiles (stage TEXT PRIMARY KEY, seconds_per_item REAL, sample_count INTEGER)"
        )
        connection.execute("INSERT INTO eta_stage_profiles VALUES ('INFERENCING', 9, 1)")

    store = JobStore(database_path)
    store.initialize()

    assert store.estimated_stage_seconds("INFERENCING", 100_000) is None


def test_replayed_prediction_does_not_duplicate_rows(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")
    store.add_image("job-1", "image.png", "image", MetadataMatch("not_provided", {}))
    prediction = rank_prediction(
        "image.png",
        scores((17, 0.39), (15, 0.35), (0, 0.26)),
    )

    store.record_prediction("job-1", prediction)
    JobStore(tmp_path / "app.db").record_prediction("job-1", prediction)

    assert len(store.predictions("job-1")) == 1
    assert store.counts("job-1") == {
        "discovered": 1,
        "predicted": 1,
        "low_confidence": 1,
        "rejected": 0,
    }


def test_metadata_joins_before_selecting_explicit_id() -> None:
    match = join_metadata(
        ["nested/image.png"],
        [
            {"image_id": "nested/image", "source_relative_path": "wrong.png"},
            {"image_id": "PATH-WINS", "source_relative_path": "nested/image.png"},
        ],
    )["nested/image.png"]

    assert match.status == "matched"
    assert match.explicit_id == "PATH-WINS"


def test_source_images_are_registered_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(5):
        (source / f"image-{index}.png").write_bytes(b"candidate")
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")
    original_connection = store._connection
    connection_count = 0

    @contextmanager
    def counted_connection() -> Iterator[sqlite3.Connection]:
        nonlocal connection_count
        connection_count += 1
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(store, "_connection", counted_connection)

    paths, rejected = register_source_images(store, "job-1", source, batch_size=2)

    assert len(paths) == 5
    assert rejected == frozenset()
    assert connection_count == 3


def test_partial_registration_is_cleared_before_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(5):
        (source / f"image-{index}.png").write_bytes(b"candidate")
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")
    for state in (JobState.SCANNING, JobState.IMPORTING, JobState.VALIDATING):
        store.transition("job-1", state)
    original_add_images = store.add_images
    call_count = 0

    def fail_second_batch(
        job_id: str,
        images: list[tuple[str, str | None, MetadataMatch]],
    ) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("interrupted batch")
        original_add_images(job_id, images)

    monkeypatch.setattr(store, "add_images", fail_second_batch)
    with pytest.raises(RuntimeError, match="interrupted batch"):
        register_source_images(store, "job-1", source, batch_size=2)
    assert store.counts("job-1")["discovered"] == 2

    store.clear_validation_results("job-1")
    monkeypatch.setattr(store, "add_images", original_add_images)
    register_source_images(store, "job-1", source, batch_size=2)

    assert store.counts("job-1")["discovered"] == 5


def test_duplicate_rejections_use_the_registration_batch_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    paths = [f"image-{index}.png" for index in range(5)]
    for path in paths:
        (source / path).write_bytes(b"candidate")
    metadata = [
        {"source_relative_path": path, "photo_id": "duplicate"} for path in paths
    ]
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")
    original_connection = store._connection
    connection_count = 0

    @contextmanager
    def counted_connection() -> Iterator[sqlite3.Connection]:
        nonlocal connection_count
        connection_count += 1
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(store, "_connection", counted_connection)

    _, rejected = register_source_images(
        store,
        "job-1",
        source,
        metadata,
        batch_size=2,
    )

    assert rejected == frozenset(paths)
    assert connection_count == 6


def test_fake_job_writes_consistent_outputs(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    (source / "nested").mkdir()
    for relative_path in ("a.png", "nested/b.jpg", "duplicate.jpg", "duplicate.png"):
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")

    score_map = {
        "a.png": scores((15, 0.8), (0, 0.1), (1, 0.1)),
        "nested/b.jpg": scores((17, 0.39), (15, 0.35), (0, 0.26)),
        "duplicate.jpg": scores((0, 0.8), (1, 0.1), (2, 0.1)),
        "duplicate.png": scores((0, 0.8), (1, 0.1), (2, 0.1)),
    }
    metadata = [
        {
            "object_id": "0001",
            "source_relative_path": "a.png",
            "notes": 'comma, quote " and\nnewline',
        }
    ]

    result = run_fake_job_with_outputs(
        source,
        score_map,
        tmp_path / "job",
        metadata_rows=metadata,
        job_id="job-1",
    )

    assert JobStore(result.database_path).job(result.job_id).state == JobState.COMPLETED
    assert sorted(path.name for path in result.output_directory.iterdir()) == [
        "counts_summary.csv",
        "job_summary.json",
        "low_confidence_top3.csv",
        "predictions_top1.csv",
        "rejected_images.csv",
    ]
    with (result.output_directory / "predictions_top1.csv").open(newline="") as handle:
        predictions = list(csv.DictReader(handle))
    with (result.output_directory / "low_confidence_top3.csv").open(newline="") as handle:
        low_confidence = list(csv.DictReader(handle))
    with (result.output_directory / "rejected_images.csv").open(newline="") as handle:
        rejected = list(csv.DictReader(handle))
    summary = json.loads((result.output_directory / "job_summary.json").read_text())

    assert len(predictions) == 2
    assert len(low_confidence) == 1
    assert len(rejected) == 2
    assert list(predictions[0]) == [
        "photo_id",
        "source_relative_path",
        "original_filename",
        "target_id",
        "target_display_name",
        "softmax_score",
        "metadata_status",
        "metadata__notes",
        "metadata__object_id",
        "metadata__source_relative_path",
    ]
    assert list(low_confidence[0]) == [
        "photo_id",
        "source_relative_path",
        "original_filename",
        "rank_1_target_id",
        "rank_1_display_name",
        "rank_1_softmax_score",
        "rank_2_target_id",
        "rank_2_display_name",
        "rank_2_softmax_score",
        "rank_3_target_id",
        "rank_3_display_name",
        "rank_3_softmax_score",
    ]
    assert list(rejected[0]) == [
        "photo_id",
        "source_relative_path",
        "original_filename",
        "rejection_stage",
        "rejection_reason",
        "detail",
    ]
    assert predictions[0]["photo_id"] == "0001"
    assert predictions[0]["metadata__notes"] == 'comma, quote " and\nnewline'
    assert float(predictions[0]["softmax_score"]) == 0.8
    assert predictions[1]["metadata_status"] == "unmatched"
    assert low_confidence[0] == {
        "photo_id": "nested/b",
        "source_relative_path": "nested/b.jpg",
        "original_filename": "b.jpg",
        "rank_1_target_id": "582420",
        "rank_1_display_name": "Rhizaria",
        "rank_1_softmax_score": "0.390000",
        "rank_2_target_id": "370437",
        "rank_2_display_name": "Heterokontophyta",
        "rank_2_softmax_score": "0.350000",
        "rank_3_target_id": "11",
        "rank_3_display_name": "Ciliophora",
        "rank_3_softmax_score": "0.260000",
    }
    assert {row["rejection_reason"] for row in rejected} == {"duplicate_photo_id"}
    assert {row["rejection_stage"] for row in rejected} == {"validation"}
    completed_job = JobStore(result.database_path).job(result.job_id)
    assert summary == {
        "schema_version": "1.0",
        "job_id": "job-1",
        "status": "COMPLETED",
        "started_at": completed_job.started_at,
        "completed_at": completed_job.completed_at,
        "model_name": "fake-model",
        "checkpoint_sha256": "fake-checkpoint",
        "score_type": "uncalibrated_softmax",
        "low_confidence_threshold": 0.4,
        "usb_ejected": False,
        "counts": {
            "discovered": 4,
            "predicted": 2,
            "low_confidence": 1,
            "rejected": 2,
        },
    }
    assert not (result.output_directory / "README.md").exists()


def test_exact_threshold_is_not_written_to_low_confidence_csv(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    (source / "boundary.png").write_bytes(b"fake")
    (source / "low.png").write_bytes(b"fake")

    result = run_fake_job_with_outputs(
        source,
        {
            "boundary.png": scores((0, 0.4), (1, 0.35), (2, 0.25)),
            "low.png": scores((0, 0.399999), (1, 0.35), (2, 0.250001)),
        },
        tmp_path / "job",
        job_id="job-1",
    )

    with (result.output_directory / "predictions_top1.csv").open(newline="") as handle:
        predictions = list(csv.DictReader(handle))
    with (result.output_directory / "low_confidence_top3.csv").open(newline="") as handle:
        low_confidence = list(csv.DictReader(handle))

    assert [row["source_relative_path"] for row in predictions] == ["boundary.png", "low.png"]
    assert [row["source_relative_path"] for row in low_confidence] == ["low.png"]


def test_published_outputs_are_group_readable_without_world_access(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    (source / "image.png").write_bytes(b"fake")
    previous_umask = os.umask(0o007)
    try:
        result = run_fake_job_with_outputs(
            source,
            {"image.png": scores((0, 0.8), (1, 0.1), (2, 0.1))},
            tmp_path / "job",
            job_id="job-1",
        )
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(result.output_directory.stat().st_mode) == 0o2750
    for output in result.output_directory.iterdir():
        assert stat.S_IMODE(output.stat().st_mode) == 0o640
