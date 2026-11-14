"""Acceptance tests for the additive count CSV; no GPU, USB or live services."""

import csv
import json
from pathlib import Path

import pytest
from plankton_core import CLASS_TARGETS, JobStore, run_fake_job_with_outputs, write_outputs

TARGETS = (
    ("11", "Ciliophora"),
    ("51", "Mollusca"),
    ("558", "Porifera"),
    ("799", "Nematoda"),
    ("801", "Chlorophyta"),
    ("882", "Annelida"),
    ("1065", "Arthropoda"),
    ("1267", "Cnidaria"),
    ("1806", "Echinodermata"),
    ("1821", "Chordata"),
    ("14260", "Rotifera"),
    ("146537", "Cyanobacteria"),
    ("345465", "Ochrophyta"),
    ("368664", "Charophyta"),
    ("369190", "Haptophyta"),
    ("370437", "Heterokontophyta (Diatom)"),
    ("450030", "Myzozoa (Dinoflagellate)"),
    ("582420", "Rhizaria"),
    ("ntc_028", "egg/cyst"),
    ("ntc_082", "detritus"),
)


def scores(*values: tuple[int, float]) -> list[float]:
    result = [0.0] * 20
    for index, value in values:
        result[index] = value
    return result


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def mixed_output(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    predictions = {
        "high.png": scores((16, 0.8), (15, 0.1), (0, 0.1)),
        "boundary.png": scores((15, 0.4), (16, 0.35), (0, 0.25)),
        "rounded.png": scores((15, 0.3999999), (16, 0.35), (0, 0.2500001)),
        "tie.png": scores((15, 0.35), (16, 0.35), (0, 0.3)),
        "duplicate.jpg": scores((0, 1.0)),
        "duplicate.png": scores((0, 1.0)),
    }
    for name in predictions:
        (source / name).write_bytes(b"fake")
    return run_fake_job_with_outputs(
        source, predictions, tmp_path / "job", job_id="counts-test"
    ).output_directory


def test_new_bundle_replaces_only_result_readme(mixed_output: Path) -> None:
    assert {p.name for p in mixed_output.iterdir()} == {
        "predictions_top1.csv",
        "low_confidence_top3.csv",
        "rejected_images.csv",
        "counts_summary.csv",
        "job_summary.json",
    }


def test_summary_rows_all_classes_and_raw_score_counts(mixed_output: Path) -> None:
    rows = read_csv(mixed_output / "counts_summary.csv")
    assert list(rows[0]) == ["name", "target_id", "total_count", "low_confidence_count"]
    assert [list(row.values()) for row in rows[:3]] == [
        ["All images", "", "6", ""],
        ["Predicted images", "", "4", "2"],
        ["Rejected images", "", "2", ""],
    ]
    assert [(row["target_id"], row["name"]) for row in rows[3:]] == list(TARGETS)
    for index, row in enumerate(rows[3:]):
        assert (row["total_count"], row["low_confidence_count"]) == (
            ("3", "2") if index == 15 else ("1", "0") if index == 16 else ("0", "0")
        )
    counts = json.loads((mixed_output / "job_summary.json").read_text())["counts"]
    assert counts == {"discovered": 6, "predicted": 4, "low_confidence": 2, "rejected": 2}
    assert sum(int(row["total_count"]) for row in rows[3:]) == counts["predicted"]
    assert sum(int(row["low_confidence_count"]) for row in rows[3:]) == counts["low_confidence"]


def test_existing_prediction_names_and_routing_are_unchanged(mixed_output: Path) -> None:
    top1 = read_csv(mixed_output / "predictions_top1.csv")
    low = read_csv(mixed_output / "low_confidence_top3.csv")
    assert {row["target_display_name"] for row in top1} == {"Heterokontophyta", "Myzozoa"}
    assert {row["rank_1_display_name"] for row in low} == {"Heterokontophyta"}
    assert {row["rank_2_display_name"] for row in low} == {"Myzozoa"}
    assert {row["source_relative_path"] for row in low} == {"rounded.png", "tie.png"}
    assert (
        next(row for row in top1 if row["source_relative_path"] == "rounded.png")["softmax_score"]
        == "0.400000"
    )
    # The canonical mapping is not the presentation alias mapping.
    assert "Diatom" not in repr(CLASS_TARGETS)
    assert "Dinoflagellate" not in repr(CLASS_TARGETS)


def test_json_summary_contract_is_unchanged(mixed_output: Path) -> None:
    job = JobStore(mixed_output.parent / "app.db").job("counts-test")
    assert json.loads((mixed_output / "job_summary.json").read_text()) == {
        "schema_version": "1.0",
        "job_id": "counts-test",
        "status": "COMPLETED",
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "model_name": "fake-model",
        "checkpoint_sha256": "fake-checkpoint",
        "score_type": "uncalibrated_softmax",
        "low_confidence_threshold": 0.4,
        "usb_ejected": False,
        "counts": {"discovered": 6, "predicted": 4, "low_confidence": 2, "rejected": 2},
    }


@pytest.mark.parametrize("rejected", [False, True], ids=["empty", "all-rejected"])
def test_no_predictions_still_lists_all_classes(tmp_path: Path, rejected: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    if rejected:
        for name in ("duplicate.jpg", "duplicate.png"):
            (source / name).write_bytes(b"fake")
    output = run_fake_job_with_outputs(source, {}, tmp_path / "job").output_directory
    rows = read_csv(output / "counts_summary.csv")
    assert len(rows) == 23
    assert [row["total_count"] for row in rows[:3]] == [
        "2" if rejected else "0",
        "0",
        "2" if rejected else "0",
    ]
    assert [row["low_confidence_count"] for row in rows[:3]] == ["", "0", ""]
    assert [(row["target_id"], row["name"]) for row in rows[3:]] == list(TARGETS)
    assert all(row["total_count"] == row["low_confidence_count"] == "0" for row in rows[3:])


def test_repeat_output_is_deterministic_and_cannot_overwrite(mixed_output: Path) -> None:
    store = JobStore(mixed_output.parent / "app.db")
    summary = json.loads((mixed_output / "job_summary.json").read_text())
    before = {path.name: path.read_bytes() for path in mixed_output.iterdir()}
    repeat = mixed_output.parent / "repeat"
    write_outputs(store, "counts-test", repeat, summary["completed_at"])
    assert {p.name: p.read_bytes() for p in repeat.iterdir()} == before
    with pytest.raises(FileExistsError):
        write_outputs(store, "counts-test", mixed_output, summary["completed_at"])
    assert {p.name: p.read_bytes() for p in mixed_output.iterdir()} == before


def test_count_write_failure_cannot_publish_partial_bundle(
    mixed_output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.open
    original_files = {p.name: p.read_bytes() for p in mixed_output.iterdir()}
    destination = mixed_output.parent / "failed-output"

    def fail_counts(path: Path, *args: object, **kwargs: object) -> object:
        if path.name == "counts_summary.csv":
            raise OSError("injected statistics write failure")
        return original(path, *args, **kwargs)  # type: ignore[call-overload]

    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", fail_counts)
        with pytest.raises(OSError, match="injected statistics write failure"):
            write_outputs(
                JobStore(mixed_output.parent / "app.db"),
                "counts-test",
                destination,
                "2026-09-12T00:00:00+00:00",
            )
    assert not destination.exists()
    assert {p.name: p.read_bytes() for p in mixed_output.iterdir()} == original_files
