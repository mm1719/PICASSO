from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Protocol, cast

import timm
import torch
from PIL import Image, ImageOps
from plankton_core import CLASS_TARGETS
from torch import nn
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms

MODEL_NAME = "convnextv2_large.fcmae_ft_in22k_in1k"
DEFAULT_GPU_LOCK_PATH = Path("/run/plankton-inference/gpu.lock")
DEFAULT_TORCH_CACHE_PATH = Path("/var/lib/plankton-inference/torch-cache")


class CheckpointIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelSettings:
    checkpoint_path: Path
    checkpoint_sha256: str
    torch_cache_path: Path = DEFAULT_TORCH_CACHE_PATH
    batch_size: int = 192
    workers: int = 4

    @classmethod
    def from_environment(cls) -> ModelSettings:
        try:
            checkpoint_path = Path(os.environ["PLANKTON_CHECKPOINT_PATH"])
            checkpoint_sha256 = os.environ["PLANKTON_CHECKPOINT_SHA256"].lower()
        except KeyError as error:
            raise ValueError(f"Missing required environment variable: {error.args[0]}") from error
        if len(checkpoint_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in checkpoint_sha256
        ):
            raise ValueError("PLANKTON_CHECKPOINT_SHA256 must be a 64-character hexadecimal digest")
        return cls(
            checkpoint_path=checkpoint_path,
            checkpoint_sha256=checkpoint_sha256,
            torch_cache_path=Path(
                os.environ.get("PLANKTON_TORCH_CACHE_PATH", DEFAULT_TORCH_CACHE_PATH)
            ),
        )


def checkpoint_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class GpuLock:
    def __init__(self, path: Path = DEFAULT_GPU_LOCK_PATH) -> None:
        self.path = path

    @contextmanager
    def try_acquire(self) -> Iterator[bool]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle: IO[str] = self.path.open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class BatchInferenceAdapter(Protocol):
    def predict_paths(self, relative_paths: Sequence[str]) -> dict[str, tuple[float, ...]]: ...


def infer_when_gpu_available(
    gpu_lock: GpuLock,
    adapter: BatchInferenceAdapter,
    relative_paths: Sequence[str],
) -> dict[str, tuple[float, ...]] | None:
    with gpu_lock.try_acquire() as acquired:
        if not acquired:
            return None
        return adapter.predict_paths(relative_paths)


def make_preprocess() -> Callable[[Image.Image], torch.Tensor]:
    return cast(
        Callable[[Image.Image], torch.Tensor],
        transforms.Compose(
            [
                transforms.Resize(
                    146,
                    interpolation=transforms.InterpolationMode.BICUBIC,
                    antialias=True,
                ),
                transforms.CenterCrop(128),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=(0.485, 0.456, 0.406),
                    std=(0.229, 0.224, 0.225),
                ),
            ]
        ),
    )


class ImageDataset(Dataset[tuple[str, torch.Tensor]]):
    def __init__(
        self,
        source_root: Path,
        relative_paths: Sequence[str],
        preprocess: Callable[[Image.Image], torch.Tensor],
    ) -> None:
        self.source_root = source_root.resolve()
        self.relative_paths = tuple(relative_paths)
        self.preprocess = preprocess

    def __len__(self) -> int:
        return len(self.relative_paths)

    def __getitem__(self, index: int) -> tuple[str, torch.Tensor]:
        relative_path = self.relative_paths[index]
        image_path = (self.source_root / PurePosixPath(relative_path)).resolve()
        if not image_path.is_relative_to(self.source_root):
            raise ValueError(f"Image path escapes source root: {relative_path}")
        with Image.open(image_path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            return relative_path, self.preprocess(image)


class TorchInferenceAdapter:
    def __init__(
        self,
        source_root: Path,
        settings: ModelSettings,
        *,
        device: str = "cuda",
        compile_model: bool = True,
    ) -> None:
        self.source_root = source_root
        self.settings = settings
        self.device = torch.device(device)
        self.compile_model = compile_model
        self.preprocess = make_preprocess()
        self._model: nn.Module | None = None

    def _load_model(self) -> nn.Module:
        if self._model is not None:
            return self._model
        actual_hash = checkpoint_sha256(self.settings.checkpoint_path)
        if actual_hash != self.settings.checkpoint_sha256:
            raise CheckpointIntegrityError(
                f"Checkpoint SHA-256 mismatch: expected {self.settings.checkpoint_sha256}, "
                f"got {actual_hash}"
            )
        checkpoint = cast(
            dict[str, object],
            torch.load(self.settings.checkpoint_path, map_location="cpu", weights_only=False),
        )
        expected_target_ids = [target_id for target_id, _ in CLASS_TARGETS]
        expected_names = [name for _, name in CLASS_TARGETS]
        if checkpoint.get("target_ids") != expected_target_ids:
            raise CheckpointIntegrityError("Checkpoint target_ids do not match the serving mapping")
        if checkpoint.get("class_names") != expected_names:
            raise CheckpointIntegrityError(
                "Checkpoint class_names do not match the serving mapping"
            )
        state = checkpoint.get("model")
        if not isinstance(state, Mapping):
            raise CheckpointIntegrityError("Checkpoint has no model state")

        model = timm.create_model(
            MODEL_NAME,
            pretrained=False,
            num_classes=len(CLASS_TARGETS),
            drop_path_rate=0.15,
        )
        incompatible = model.load_state_dict(state)
        if incompatible.missing_keys or incompatible.unexpected_keys:
            raise CheckpointIntegrityError(f"Checkpoint state mismatch: {incompatible}")
        model.eval().to(self.device)
        if self.compile_model:
            if self.device.type != "cuda":
                raise ValueError("torch.compile production path requires a CUDA device")
            self.settings.torch_cache_path.mkdir(parents=True, exist_ok=True)
            os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(self.settings.torch_cache_path)
            model = cast(nn.Module, torch.compile(model, mode="reduce-overhead"))
        self._model = model
        return model

    def scores_for(self, source_relative_path: str) -> Sequence[float]:
        return self.predict_paths((source_relative_path,))[source_relative_path]

    def predict_paths(self, relative_paths: Sequence[str]) -> dict[str, tuple[float, ...]]:
        if not relative_paths:
            return {}
        model = self._load_model()
        dataset = ImageDataset(self.source_root, relative_paths, self.preprocess)
        loader = DataLoader(
            dataset,
            batch_size=self.settings.batch_size,
            shuffle=False,
            num_workers=self.settings.workers,
            pin_memory=self.device.type == "cuda",
            persistent_workers=self.settings.workers > 0,
        )
        predictions: dict[str, tuple[float, ...]] = {}
        with torch.inference_mode():
            for paths, tensors in loader:
                batch_paths = cast(Sequence[str], paths)
                batch = cast(torch.Tensor, tensors).to(
                    self.device,
                    non_blocking=self.device.type == "cuda",
                )
                autocast = (
                    torch.autocast("cuda", dtype=torch.float16)
                    if self.device.type == "cuda"
                    else nullcontext()
                )
                with autocast:
                    scores = torch.softmax(model(batch), dim=1).float().cpu()
                for path, row in zip(batch_paths, scores, strict=True):
                    predictions[path] = tuple(float(score) for score in row)
        return predictions
