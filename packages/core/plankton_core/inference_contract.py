from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Protocol

CLASS_TARGETS = (
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
LOW_CONFIDENCE_THRESHOLD = 0.4
IMAGE_SUFFIXES = frozenset({".jpeg", ".jpg", ".png"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"


class InvalidModelOutput(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RankedTarget:
    rank: int
    class_index: int
    target_id: str
    display_name: str
    softmax_score: float


@dataclass(frozen=True, slots=True)
class Prediction:
    source_relative_path: str
    top1: RankedTarget
    low_confidence_top3: tuple[RankedTarget, ...]


class UsbAdapter(Protocol):
    def image_paths(self) -> tuple[str, ...]: ...


class InferenceAdapter(Protocol):
    def scores_for(self, source_relative_path: str) -> Sequence[float]: ...


class FakeUsbAdapter:
    def __init__(self, root: Path) -> None:
        self.root = root

    def image_paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and is_image_candidate(path)
            )
        )


class FakeInferenceAdapter:
    def __init__(self, scores_by_path: Mapping[str, Sequence[float]]) -> None:
        self.scores_by_path = scores_by_path

    def scores_for(self, source_relative_path: str) -> Sequence[float]:
        return self.scores_by_path[source_relative_path]


def is_image_candidate(path: Path) -> bool:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            signature = handle.read(len(PNG_SIGNATURE))
    except OSError:
        return False
    return signature.startswith((PNG_SIGNATURE, JPEG_SIGNATURE))


def rank_prediction(source_relative_path: str, scores: Sequence[float]) -> Prediction:
    if len(scores) != len(CLASS_TARGETS):
        raise ValueError(f"Expected {len(CLASS_TARGETS)} scores, got {len(scores)}")
    if any(not isfinite(float(score)) for score in scores):
        raise InvalidModelOutput("Softmax scores must be finite")
    if any(not 0 <= score <= 1 for score in scores):
        raise ValueError("Softmax scores must be finite values between 0 and 1")

    indexes = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    ranked = tuple(
        RankedTarget(rank, index, *CLASS_TARGETS[index], float(scores[index]))
        for rank, index in enumerate(indexes[:3], start=1)
    )
    return Prediction(
        source_relative_path=source_relative_path,
        top1=ranked[0],
        low_confidence_top3=ranked if ranked[0].softmax_score < LOW_CONFIDENCE_THRESHOLD else (),
    )


def run_fake_job(usb: UsbAdapter, model: InferenceAdapter) -> tuple[Prediction, ...]:
    return tuple(rank_prediction(path, model.scores_for(path)) for path in usb.image_paths())
