from __future__ import annotations

from pathlib import Path

import pytest
from plankton_core import (
    CLASS_TARGETS,
    InvalidModelOutput,
    JobState,
    JobStore,
    RuntimeMode,
    is_image_candidate,
    jobs,
    join_metadata,
    rank_prediction,
)


def _scores(*values: tuple[int, float]) -> list[float]:
    scores = [0.0] * len(CLASS_TARGETS)
    for index, score in values:
        scores[index] = score
    return scores


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_model_output_is_rejected(invalid: float) -> None:
    with pytest.raises(InvalidModelOutput):
        rank_prediction("bad.png", _scores((0, invalid)))


@pytest.mark.parametrize("invalid", [-0.001, 1.001])
def test_out_of_range_model_output_is_rejected(invalid: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        rank_prediction("bad.png", _scores((0, invalid)))


def test_softmax_score_of_one_is_valid() -> None:
    assert rank_prediction("certain.png", _scores((0, 1.0))).top1.softmax_score == 1.0


def test_image_candidate_boundary_handles_jpg_signature_and_unreadable_path(
    tmp_path: Path,
) -> None:
    jpg = tmp_path / "image.jpg"
    jpg.write_bytes(b"extension-only")
    signature_only = tmp_path / "image.bin"
    signature_only.write_bytes(b"\xff\xd8\xfffixture")

    assert is_image_candidate(jpg)
    assert is_image_candidate(signature_only)
    assert not is_image_candidate(tmp_path / "missing.bin")


def test_top_k_ties_use_class_index_and_preserve_emitted_scores() -> None:
    prediction = rank_prediction(
        "tie.png",
        _scores((2, 0.3), (1, 0.3), (0, 0.3), (3, 0.1)),
    )

    assert [rank.class_index for rank in prediction.low_confidence_top3] == [0, 1, 2]
    assert [rank.softmax_score for rank in prediction.low_confidence_top3] == [0.3] * 3


def test_illegal_and_terminal_state_transitions_leave_state_unchanged(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")

    with pytest.raises(ValueError, match="Invalid job transition"):
        store.transition("job-1", JobState.INFERENCING)
    assert store.job("job-1").state == JobState.CREATED

    store.transition("job-1", JobState.FAILED)
    with pytest.raises(ValueError, match="Terminal job"):
        store.transition("job-1", JobState.SCANNING)
    assert store.job("job-1").state == JobState.FAILED


def test_public_state_values_and_transition_graph_are_exact() -> None:
    assert tuple(JobState) == (
        JobState.CREATED,
        JobState.SCANNING,
        JobState.IMPORTING,
        JobState.VALIDATING,
        JobState.WAITING_FOR_GPU,
        JobState.INFERENCING,
        JobState.GENERATING_OUTPUTS,
        JobState.ARCHIVING,
        JobState.EXPORTING,
        JobState.WAITING_FOR_USB,
        JobState.COMPLETED,
        JobState.SECURITY_BLOCKED,
        JobState.FAILED,
    )
    assert [state.value for state in JobState] == [
        "CREATED",
        "SCANNING",
        "IMPORTING",
        "VALIDATING",
        "WAITING_FOR_GPU",
        "INFERENCING",
        "GENERATING_OUTPUTS",
        "ARCHIVING",
        "EXPORTING",
        "WAITING_FOR_USB",
        "COMPLETED",
        "SECURITY_BLOCKED",
        "FAILED",
    ]
    assert tuple(RuntimeMode) == (RuntimeMode.INFERENCE_ENABLED, RuntimeMode.TRAINING)
    assert [mode.value for mode in RuntimeMode] == ["inference_enabled", "training"]
    assert jobs.TERMINAL_STATES == {
        JobState.COMPLETED,
        JobState.SECURITY_BLOCKED,
        JobState.FAILED,
    }
    assert jobs.ALLOWED_TRANSITIONS == {
        JobState.CREATED: {JobState.SCANNING},
        JobState.SCANNING: {
            JobState.IMPORTING,
            JobState.SECURITY_BLOCKED,
            JobState.WAITING_FOR_USB,
        },
        JobState.IMPORTING: {
            JobState.VALIDATING,
            JobState.SECURITY_BLOCKED,
            JobState.WAITING_FOR_USB,
        },
        JobState.VALIDATING: {JobState.WAITING_FOR_GPU},
        JobState.WAITING_FOR_GPU: {JobState.INFERENCING},
        JobState.INFERENCING: {
            JobState.WAITING_FOR_GPU,
            JobState.GENERATING_OUTPUTS,
        },
        JobState.GENERATING_OUTPUTS: {JobState.ARCHIVING},
        JobState.ARCHIVING: {JobState.EXPORTING},
        JobState.EXPORTING: {JobState.COMPLETED, JobState.WAITING_FOR_USB},
        JobState.WAITING_FOR_USB: {
            JobState.SCANNING,
            JobState.IMPORTING,
            JobState.EXPORTING,
        },
    }


def test_metadata_path_precedes_filename_and_duplicate_path_is_ambiguous() -> None:
    matches = join_metadata(
        ["nested/image.png", "other/image.png"],
        [
            {"source_relative_path": "nested\\image.png", "image_id": "first"},
            {"source_relative_path": "nested/image.png", "image_id": "second"},
            {"filename": "image.png", "image_id": "filename-only"},
        ],
    )

    assert matches["nested/image.png"].status == "ambiguous"
    assert matches["other/image.png"].status == "unmatched"


@pytest.mark.parametrize(
    ("path_column", "id_column"),
    [
        ("source_relative_path", "image_id"),
        ("relative_path", "object_id"),
        ("image_path", "photo_id"),
    ],
)
def test_metadata_join_accepts_documented_path_and_id_aliases(
    path_column: str, id_column: str
) -> None:
    match = join_metadata(
        ["nested/image.png"],
        [{path_column: "nested\\image.png", id_column: "EXPLICIT-ID"}],
    )["nested/image.png"]

    assert (match.status, match.explicit_id) == ("matched", "EXPLICIT-ID")


@pytest.mark.parametrize("filename_column", ["original_filename", "filename", "file_name"])
def test_metadata_join_accepts_a_unique_documented_filename_alias(filename_column: str) -> None:
    match = join_metadata(
        ["nested/image.png"],
        [{filename_column: "image.png", "photo_id": "FILENAME-ID"}],
    )["nested/image.png"]

    assert (match.status, match.explicit_id) == ("matched", "FILENAME-ID")


def test_metadata_absence_is_explicitly_not_provided() -> None:
    match = join_metadata(["nested/image.png"], None)["nested/image.png"]

    assert (match.status, match.values, match.explicit_id) == ("not_provided", {}, None)


def test_job_store_rejects_missing_and_invalid_control_requests(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "app.db")
    store.initialize()
    store.create_job("job-1", "fake-model", "fake-hash")

    with pytest.raises(KeyError):
        store.request("missing")
    with pytest.raises(ValueError, match="not resumable"):
        store.waiting_job(JobState.CREATED)
    with pytest.raises(ValueError, match="cannot wait for USB"):
        store.wait_for_usb("job-1")
    with pytest.raises(ValueError, match="processed count"):
        store.set_progress("job-1", total_count=1, processed_count=2)

    assert store.progress("missing").total_count == 0
