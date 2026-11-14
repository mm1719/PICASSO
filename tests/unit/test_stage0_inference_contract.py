import json
from math import isclose
from pathlib import Path
from typing import TypedDict, cast

from plankton_core import (
    CLASS_TARGETS,
    FakeInferenceAdapter,
    FakeUsbAdapter,
    rank_prediction,
    run_fake_job,
)


class ScoreRow(TypedDict):
    class_index: int
    target_id: str
    display_name: str
    softmax_score: float


class ScoreFixture(TypedDict):
    fixture_kind: str
    softmax_scores: list[float]
    top5: list[ScoreRow]


def scores(*values: tuple[int, float]) -> list[float]:
    result = [0.0] * len(CLASS_TARGETS)
    for class_index, score in values:
        result[class_index] = score
    return result


def test_fixed_class_mapping() -> None:
    assert CLASS_TARGETS == (
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
        ("370437", "Heterokontophyta"),
        ("450030", "Myzozoa"),
        ("582420", "Rhizaria"),
        ("ntc_028", "egg/cyst"),
        ("ntc_082", "detritus"),
    )


def test_low_confidence_boundary() -> None:
    at_boundary = rank_prediction("boundary.png", scores((0, 0.4), (1, 0.35), (2, 0.25)))
    below_boundary = rank_prediction("below.png", scores((0, 0.39), (1, 0.35), (2, 0.26)))

    assert at_boundary.low_confidence_top3 == ()
    assert [item.rank for item in below_boundary.low_confidence_top3] == [1, 2, 3]


def test_fake_usb_and_model_run_recursive_slice(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.png").write_bytes(b"fake")
    (tmp_path / "b.JPEG").write_bytes(b"fake")
    (tmp_path / "ignored.txt").write_text("not an image")

    model = FakeInferenceAdapter(
        {
            "b.JPEG": scores((15, 0.8), (0, 0.1), (1, 0.1)),
            "nested/a.png": scores((17, 0.39), (15, 0.35), (0, 0.26)),
        }
    )

    predictions = run_fake_job(FakeUsbAdapter(tmp_path), model)

    assert [item.source_relative_path for item in predictions] == ["b.JPEG", "nested/a.png"]
    assert predictions[0].top1.target_id == "370437"
    assert predictions[1].top1.target_id == "582420"
    assert len(predictions[1].low_confidence_top3) == 3


def test_fake_usb_discovers_signature_only_image(tmp_path: Path) -> None:
    (tmp_path / "signature-only.bin").write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    assert FakeUsbAdapter(tmp_path).image_paths() == ("signature-only.bin",)


def test_synthetic_score_fixture_contract() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "synthetic_scores.json"
    fixture = cast(ScoreFixture, json.loads(fixture_path.read_text()))

    assert fixture["fixture_kind"] == "synthetic_not_model_output"
    assert len(fixture["softmax_scores"]) == len(CLASS_TARGETS)
    assert isclose(sum(fixture["softmax_scores"]), 1.0, abs_tol=1e-6)
    assert [row["class_index"] for row in fixture["top5"]] == [0, 1, 2, 3, 4]
    for row in fixture["top5"]:
        assert (row["target_id"], row["display_name"]) == CLASS_TARGETS[row["class_index"]]
