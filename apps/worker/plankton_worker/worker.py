from __future__ import annotations

import csv
import fcntl
import json
import logging
import math
import os
import shutil
import signal
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError
from plankton_control import (
    UsbClientError,
    UsbDeviceUnavailable,
    UsbOperations,
    UsbSecurityBlocked,
)
from plankton_core import (
    InvalidModelOutput,
    JobRequest,
    JobState,
    JobStore,
    RuntimeMode,
    rank_prediction,
    register_source_images,
    write_outputs,
)


class BatchModel(Protocol):
    def predict_paths(self, relative_paths: Sequence[str]) -> dict[str, tuple[float, ...]]: ...


class ShutdownEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> object: ...


ModelFactory = Callable[[Path], BatchModel]
FaultInjector = Callable[[str, str | None], None]
ARCHIVE_NAME = "source_images.zip"
LOGGER = logging.getLogger(__name__)
ETA_FUTURE_STAGES = {
    "SCANNING": ("IMPORTING", "VALIDATING", "INFERENCING", "FINALIZING"),
    "IMPORTING": ("VALIDATING", "INFERENCING", "FINALIZING"),
    "VALIDATING": ("INFERENCING", "FINALIZING"),
    "INFERENCING": ("FINALIZING",),
    "FINALIZING": (),
}


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_path: Path
    job_root: Path
    gpu_lock_path: Path
    checkpoint_images: int = 1536
    validation_batch_images: int = 1000
    max_image_bytes: int = 512 * 1024**2
    max_image_pixels: int = 250_000_000
    decode_timeout_seconds: int = 30


def _default_model_factory(source_root: Path) -> BatchModel:
    from plankton_inference import ModelSettings, TorchInferenceAdapter

    return TorchInferenceAdapter(source_root, ModelSettings.from_environment())


@contextmanager
def _exclusive_gpu(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o660)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _metadata_rows(staging: Path, request: JobRequest) -> list[Mapping[str, str]] | None:
    if request.metadata_relative_path is None:
        return None
    try:
        relative = PurePosixPath(request.metadata_relative_path).relative_to(
            PurePosixPath(request.source_relative_path)
        )
    except ValueError as error:
        raise RuntimeError("metadata path is outside selected source") from error
    with (staging / relative.as_posix()).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _image_rejections(
    staging: Path,
    paths: Sequence[str],
    settings: WorkerSettings,
    fault_injector: FaultInjector | None = None,
) -> dict[str, tuple[str, str]]:
    rejected: dict[str, tuple[str, str]] = {}

    def timeout_decode(_signum: int, _frame: object) -> None:
        raise TimeoutError("image decode timed out")

    for relative_path in paths:
        path = staging / relative_path
        if path.stat().st_size > settings.max_image_bytes:
            rejected[relative_path] = (
                "unreadable_image",
                "Image exceeds the configured file-size limit.",
            )
            continue
        previous_handler = signal.signal(signal.SIGALRM, timeout_decode)
        signal.setitimer(signal.ITIMER_REAL, settings.decode_timeout_seconds)
        try:
            if fault_injector is not None:
                fault_injector("decode", relative_path)
            with Image.open(path) as image:
                width, height = image.size
                if width <= 0 or height <= 0:
                    rejected[relative_path] = (
                        "invalid_dimensions",
                        "Image dimensions must be positive.",
                    )
                elif width * height > settings.max_image_pixels:
                    rejected[relative_path] = (
                        "unreadable_image",
                        "Image exceeds the configured pixel-count limit.",
                    )
                else:
                    image.load()
                    ImageOps.exif_transpose(image).convert("RGB").load()
        except TimeoutError:
            rejected[relative_path] = ("unreadable_image", "Image decode timed out.")
        except MemoryError:
            rejected[relative_path] = (
                "unreadable_image",
                "Image decoder exhausted available memory.",
            )
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            rejected[relative_path] = ("unreadable_image", "Image could not be decoded safely.")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    return rejected


def _manifest_entries(manifest_path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_entries = payload["entries"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError("import manifest is unavailable or invalid") from error
    if not isinstance(raw_entries, list):
        raise TypeError("import manifest entries must be a list")
    entries: list[str] = []
    for item in raw_entries:
        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
            raise TypeError("import manifest contains an invalid entry")
        value = str(item["relative_path"])
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts or not value or "\\" in value:
            raise RuntimeError("import manifest contains an unsafe path")
        entries.append(relative.as_posix())
    if len(entries) != len(set(entries)):
        raise RuntimeError("import manifest contains duplicate paths")
    return tuple(sorted(entries))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_archive(path: Path, manifest_path: Path) -> None:
    if path.stat().st_size <= 0:
        raise RuntimeError("source archive is empty")
    expected = {*_manifest_entries(manifest_path), "import_manifest.json"}
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(expected) or set(names) != expected:
                raise RuntimeError("source archive entries do not match import manifest")
            if archive.testzip() is not None:
                raise RuntimeError("source archive integrity check failed")
    except zipfile.BadZipFile as error:
        raise RuntimeError("source archive central directory is invalid") from error


def archive_staging(
    job_directory: Path,
    fault_injector: FaultInjector | None = None,
) -> Path:
    staging = job_directory / "staging"
    archive_directory = job_directory / "archive"
    manifest_directory = job_directory / "manifests"
    archive_path = archive_directory / ARCHIVE_NAME
    saved_manifest = manifest_directory / "import_manifest.json"

    if archive_path.exists():
        if fault_injector is not None:
            fault_injector("archive_verify", None)
        _verify_archive(archive_path, saved_manifest)
        if staging.exists():
            shutil.rmtree(staging)
        return archive_path
    source_manifest = staging / "import_manifest.json"
    entries = _manifest_entries(source_manifest)
    staging_root = staging.resolve(strict=True)
    sources: list[tuple[str, Path]] = []
    for relative_path in entries:
        source = (staging / relative_path).resolve(strict=True)
        try:
            source.relative_to(staging_root)
        except ValueError as error:
            raise RuntimeError("archive source escaped staging root") from error
        if not source.is_file():
            raise RuntimeError("archive source is not a regular file")
        sources.append((relative_path, source))

    archive_directory.mkdir(parents=True, exist_ok=True)
    manifest_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=manifest_directory, prefix=".import_manifest.", suffix=".tmp", delete=False
    ) as handle:
        manifest_temporary = Path(handle.name)
        handle.write(source_manifest.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(manifest_temporary, saved_manifest)
    _fsync_directory(manifest_directory)

    if fault_injector is not None:
        fault_injector("archive_write", None)
    with tempfile.NamedTemporaryFile(
        dir=archive_directory, prefix=f".{ARCHIVE_NAME}.", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(
            temporary, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for relative_path, source in sources:
                archive.write(source, arcname=relative_path)
            archive.write(source_manifest, arcname="import_manifest.json")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if fault_injector is not None:
            fault_injector("archive_verify", None)
        _verify_archive(temporary, saved_manifest)
        os.replace(temporary, archive_path)
        _fsync_directory(archive_directory)
    finally:
        temporary.unlink(missing_ok=True)
    shutil.rmtree(staging)
    return archive_path


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        usb: UsbOperations,
        model_factory: ModelFactory = _default_model_factory,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.settings = settings
        self.usb = usb
        self.model_factory = model_factory
        self.fault_injector = fault_injector
        self.store = JobStore(settings.database_path)
        self.store.initialize()
        self._progress_checkpoints: dict[tuple[str, str], tuple[int, float]] = {}

    @property
    def _active_job_marker(self) -> Path:
        return self.settings.gpu_lock_path.parent / "active-job"

    def _mark_active(self, job_id: str) -> None:
        self._active_job_marker.parent.mkdir(parents=True, exist_ok=True)
        self._active_job_marker.write_text(job_id + "\n", encoding="utf-8")

    def _clear_marker(self, job_id: str | None = None) -> None:
        try:
            if job_id is None or self._active_job_marker.read_text(encoding="utf-8").strip() == job_id:
                self._active_job_marker.unlink()
        except FileNotFoundError:
            pass

    def _device_id(self, request: JobRequest) -> str:
        if not request.device_uuid:
            return request.device_id
        devices = self.usb.request("list_devices")
        if not isinstance(devices, list):
            raise UsbDeviceUnavailable("USB host service returned an invalid device list")
        matches = [
            device
            for device in devices
            if isinstance(device, dict)
            and device.get("uuid") == request.device_uuid
            and (not request.device_serial or device.get("serial") == request.device_serial)
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("device_id"), str):
            raise UsbDeviceUnavailable("USB device is absent or identity is ambiguous")
        return str(matches[0]["device_id"])

    def _eta_values(
        self,
        job_id: str,
        stage: str,
        *,
        total_count: int,
        processed_count: int,
        profile_item_count: int | None = None,
    ) -> tuple[float, int | None, int | None]:
        now = time.monotonic()
        key = (job_id, stage)
        previous = self._progress_checkpoints.get(key)
        self._progress_checkpoints[key] = (processed_count, now)
        rate = 0.0
        if previous is not None:
            previous_processed, previous_at = previous
            observed = processed_count - previous_processed
            elapsed = now - previous_at
            if previous_processed > 0 and observed > 0 and elapsed > 0:
                rate = observed / elapsed
        remaining = max(total_count - processed_count, 0)
        stage_eta = math.ceil(remaining / rate) if rate > 0 else None
        profile_count = total_count if profile_item_count is None else profile_item_count
        future = [
            self.store.estimated_stage_seconds(future_stage, profile_count)
            for future_stage in ETA_FUTURE_STAGES[stage]
        ]
        job_eta = (
            stage_eta + sum(value for value in future if value is not None)
            if stage_eta is not None and all(value is not None for value in future)
            else None
        )
        return rate, stage_eta, job_eta

    def _set_timed_progress(
        self,
        job_id: str,
        stage: str,
        *,
        total_count: int,
        processed_count: int,
        total_bytes: int = 0,
        processed_bytes: int = 0,
        heartbeat: bool = False,
    ) -> None:
        if heartbeat:
            current = self.store.progress(job_id)
            self.store.set_progress(
                job_id,
                total_count=current.total_count,
                processed_count=current.processed_count,
                images_per_second=current.images_per_second,
                eta_seconds=current.eta_seconds,
                stage_eta_seconds=current.stage_eta_seconds,
                job_eta_seconds=current.job_eta_seconds,
                total_bytes=current.total_bytes,
                processed_bytes=current.processed_bytes,
                throughput_bytes_per_second=current.throughput_bytes_per_second,
                progress_unit=current.progress_unit,
            )
            return
        metric_total = total_bytes if total_bytes > 0 else total_count
        metric_processed = processed_bytes if total_bytes > 0 else processed_count
        rate, stage_eta, job_eta = self._eta_values(
            job_id,
            stage,
            total_count=metric_total,
            processed_count=metric_processed,
            profile_item_count=total_count,
        )
        self.store.set_progress(
            job_id,
            total_count=total_count,
            processed_count=processed_count,
            images_per_second=rate if total_bytes == 0 else 0,
            eta_seconds=stage_eta,
            stage_eta_seconds=stage_eta,
            job_eta_seconds=job_eta,
            total_bytes=total_bytes,
            processed_bytes=processed_bytes,
            throughput_bytes_per_second=rate if total_bytes > 0 else 0,
            progress_unit="bytes" if total_bytes > 0 else "images",
        )

    def _clear_eta(self, job_id: str) -> None:
        progress = self.store.progress(job_id)
        self.store.set_progress(
            job_id,
            total_count=progress.total_count,
            processed_count=progress.processed_count,
            total_bytes=progress.total_bytes,
            processed_bytes=progress.processed_bytes,
            throughput_bytes_per_second=progress.throughput_bytes_per_second,
            progress_unit=progress.progress_unit,
        )

    def _job_usb_request(
        self,
        request: JobRequest,
        operation: str,
        **args: object,
    ) -> Any:
        state = self.store.job(request.job_id).state
        LOGGER.info(
            "job_event job_id=%s state=%s event_code=usb_operation operation=%s "
            "outcome=started message=%s_started",
            request.job_id,
            state,
            operation,
            operation,
        )
        try:
            result = self.usb.request(operation, progress=None, **args)
        except Exception:
            LOGGER.warning(
                "job_event job_id=%s state=%s event_code=usb_operation operation=%s "
                "outcome=failed message=%s_failed",
                request.job_id,
                state,
                operation,
                operation,
            )
            raise
        LOGGER.info(
            "job_event job_id=%s state=%s event_code=usb_operation operation=%s "
            "outcome=completed message=%s_completed",
            request.job_id,
            state,
            operation,
            operation,
        )
        return result

    def _release_usb(self, request: JobRequest, device_id: str) -> Any:
        """Return a terminal non-ejected device to the desktop session."""
        return self._job_usb_request(request, "release", device_id=device_id)

    def run_once(self) -> bool:
        request = self.store.active_request()
        if request is None:
            self._clear_marker()
            return False
        active_job_id = request.job_id
        self._mark_active(active_job_id)
        state = self.store.job(active_job_id).state
        try:
            if state == JobState.CREATED:
                request = self.store.claim_created_job()
                if request is None:
                    return False
                state = JobState.SCANNING
            if state == JobState.WAITING_FOR_USB:
                state = self.store.resume_waiting_for_usb(request.job_id)
            if state in {JobState.SCANNING, JobState.IMPORTING, JobState.VALIDATING}:
                self._import(request)
            elif state in {JobState.WAITING_FOR_GPU, JobState.INFERENCING}:
                self._infer(request)
            elif state in {JobState.GENERATING_OUTPUTS, JobState.ARCHIVING}:
                self._finish(request)
            elif state in {JobState.EXPORTING, JobState.WAITING_FOR_USB}:
                self._export(request)
        finally:
            if self.store.job(active_job_id).state in {
                JobState.COMPLETED,
                JobState.SECURITY_BLOCKED,
                JobState.FAILED,
            }:
                self._clear_marker(active_job_id)
        return True

    def _import(self, request: JobRequest) -> None:
        job_directory = self.settings.job_root / request.job_id
        staging = job_directory / "staging"
        job_directory.mkdir(parents=True, exist_ok=True)
        try:
            device_id = self._device_id(request)
            state = self.store.job(request.job_id).state
            if state in {JobState.SCANNING, JobState.IMPORTING} and not staging.exists():
                import_started = time.monotonic()

                def on_scan_import_progress(
                    stage: str,
                    processed: int,
                    total: int,
                    processed_bytes: int,
                    total_bytes: int,
                    heartbeat: bool,
                ) -> None:
                    if stage == "importing" and self.store.job(request.job_id).state == JobState.SCANNING:
                        self.store.transition(request.job_id, JobState.IMPORTING)
                    self._set_timed_progress(
                        request.job_id,
                        "SCANNING" if stage == "scanning" else "IMPORTING",
                        total_count=total,
                        processed_count=processed,
                        total_bytes=total_bytes,
                        processed_bytes=processed_bytes,
                        heartbeat=heartbeat,
                    )

                self.usb.request(
                    "scan_and_import",
                    progress=on_scan_import_progress,
                    device_id=device_id,
                    selected_relative_path=request.source_relative_path,
                    staging_directory=str(staging),
                    metadata_relative_path=request.metadata_relative_path,
                )
                imported = self.store.progress(request.job_id).total_count
                self.store.record_eta_stage_timing(
                    "IMPORTING", item_count=imported, elapsed_seconds=time.monotonic() - import_started
                )
                state = JobState.IMPORTING
            if self.store.job(request.job_id).state == JobState.SCANNING:
                self.store.transition(request.job_id, JobState.IMPORTING)
            if self.store.job(request.job_id).state == JobState.IMPORTING:
                self.store.transition(request.job_id, JobState.VALIDATING)
            state = JobState.VALIDATING
            if state != JobState.VALIDATING:
                raise RuntimeError("job is not ready for validation")
            known_total = self.store.progress(request.job_id).total_count
            validation_started = time.monotonic()
            self.store.clear_validation_results(request.job_id)
            if known_total:
                self._set_timed_progress(
                    request.job_id,
                    "VALIDATING",
                    total_count=known_total,
                    processed_count=0,
                )
            paths, _ = register_source_images(
                self.store,
                request.job_id,
                staging,
                _metadata_rows(staging, request),
                batch_size=self.settings.validation_batch_images,
            )
            self._set_timed_progress(
                request.job_id,
                "VALIDATING",
                total_count=len(paths),
                processed_count=0,
            )
            for offset in range(0, len(paths), self.settings.validation_batch_images):
                batch = paths[offset : offset + self.settings.validation_batch_images]
                batch_rejections = _image_rejections(
                    staging,
                    batch,
                    self.settings,
                    self.fault_injector,
                )
                self.store.record_rejections(
                    request.job_id,
                    tuple(
                        (path, "validation", reason, detail)
                        for path, (reason, detail) in batch_rejections.items()
                    ),
                )
                self._set_timed_progress(
                    request.job_id,
                    "VALIDATING",
                    total_count=len(paths),
                    processed_count=offset + len(batch),
                )
            self.store.record_eta_stage_timing(
                "VALIDATING",
                item_count=len(paths),
                elapsed_seconds=time.monotonic() - validation_started,
            )
            self._clear_eta(request.job_id)
            self.store.transition(request.job_id, JobState.WAITING_FOR_GPU)
            self._infer(request)
        except UsbSecurityBlocked as error:
            try:
                device_id = self._device_id(request)
                self._job_usb_request(request, "eject", device_id=device_id)
            except Exception:
                LOGGER.exception(
                    "job_event job_id=%s state=%s event_code=security_eject_failed",
                    request.job_id,
                    self.store.job(request.job_id).state,
                )
            self.store.set_failure(
                request.job_id,
                "malware_detected",
                "Import stopped because the security scan found a threat.",
                error.relative_paths,
            )
            LOGGER.warning(
                "job_event job_id=%s state=%s event_code=malware_detected message=import_blocked",
                request.job_id,
                self.store.job(request.job_id).state,
            )
            self.store.transition(request.job_id, JobState.SECURITY_BLOCKED)
        except UsbDeviceUnavailable:
            self.store.wait_for_usb(request.job_id)
        except Exception:  # noqa: BLE001 - worker boundary records terminal job failure
            self._fail(request, "import_failed", "Job failed during secure import or validation.")

    def _fail(self, request: JobRequest, code: str, message: str) -> None:
        LOGGER.exception(
            "job_event job_id=%s state=%s event_code=%s message=worker_failed",
            request.job_id,
            self.store.job(request.job_id).state,
            code,
        )
        self.store.set_failure(request.job_id, code, message)
        try:
            device_id = self._device_id(request)
            self._release_usb(request, device_id)
        except Exception:
            LOGGER.exception(
                "job_event job_id=%s state=%s event_code=native_handoff_failed",
                request.job_id,
                self.store.job(request.job_id).state,
            )
        if self.store.job(request.job_id).state not in {
            JobState.FAILED,
            JobState.SECURITY_BLOCKED,
            JobState.COMPLETED,
        }:
            self.store.transition(request.job_id, JobState.FAILED)

    def _infer(self, request: JobRequest) -> None:
        if self.store.runtime_mode() != RuntimeMode.INFERENCE_ENABLED:
            if self.store.job(request.job_id).state == JobState.INFERENCING:
                self._clear_eta(request.job_id)
                self.store.transition(request.job_id, JobState.WAITING_FOR_GPU)
            return
        staging = self.settings.job_root / request.job_id / "staging"
        source_paths = self.store.pending_image_paths(request.job_id)
        with _exclusive_gpu(self.settings.gpu_lock_path) as acquired:
            if not acquired:
                if self.store.job(request.job_id).state == JobState.INFERENCING:
                    self._clear_eta(request.job_id)
                    self.store.transition(request.job_id, JobState.WAITING_FOR_GPU)
                return
            try:
                self.store.transition(request.job_id, JobState.INFERENCING)
                progress = self.store.progress(request.job_id)
                counts = self.store.counts(request.job_id)
                initial_processed = counts["predicted"] + counts["rejected"]
                started = time.monotonic()
                self._set_timed_progress(
                    request.job_id,
                    "INFERENCING",
                    total_count=progress.total_count,
                    processed_count=initial_processed,
                )
                processed = initial_processed
                model = self.model_factory(staging) if source_paths else None
                for offset in range(0, len(source_paths), self.settings.checkpoint_images):
                    batch = source_paths[offset : offset + self.settings.checkpoint_images]
                    if model is None:
                        raise RuntimeError("inference model is unavailable")
                    scores = model.predict_paths(batch)
                    predictions = []
                    for path in batch:
                        try:
                            predictions.append(rank_prediction(path, scores[path]))
                        except InvalidModelOutput:
                            self.store.record_rejection(
                                request.job_id,
                                path,
                                "inference",
                                "invalid_model_output",
                                "Model output contained a non-finite value.",
                            )
                    processed += len(batch)
                    rate, eta, job_eta = self._eta_values(
                        request.job_id,
                        "INFERENCING",
                        total_count=progress.total_count,
                        processed_count=processed,
                    )
                    self.store.checkpoint_predictions(
                        request.job_id,
                        predictions,
                        total_count=progress.total_count,
                        processed_count=processed,
                        images_per_second=rate,
                        eta_seconds=eta,
                        stage_eta_seconds=eta,
                        job_eta_seconds=job_eta,
                    )
                self.store.record_eta_stage_timing(
                    "INFERENCING",
                    item_count=len(source_paths),
                    elapsed_seconds=time.monotonic() - started,
                )
                self.store.transition(request.job_id, JobState.GENERATING_OUTPUTS)
                finalizing_started = time.monotonic()
                finalizing_eta = self.store.estimated_stage_seconds(
                    "FINALIZING", progress.total_count
                )
                self.store.set_progress(
                    request.job_id,
                    total_count=progress.total_count,
                    processed_count=processed,
                    eta_seconds=finalizing_eta,
                    stage_eta_seconds=finalizing_eta,
                    job_eta_seconds=finalizing_eta,
                )
                self._finish(request)
                if self.store.job(request.job_id).state == JobState.COMPLETED:
                    self.store.record_eta_stage_timing(
                        "FINALIZING",
                        item_count=progress.total_count,
                        elapsed_seconds=time.monotonic() - finalizing_started,
                    )
                    self._clear_eta(request.job_id)
            except Exception:  # noqa: BLE001 - worker boundary records terminal job failure
                self._fail(
                    request,
                    "inference_failed",
                    "Job failed during model inference or output generation.",
                )

    def _finish(self, request: JobRequest) -> None:
        try:
            state = self.store.job(request.job_id).state
            output_directory = self.settings.job_root / request.job_id / "outputs"
            completed_at: str | None = None
            if state == JobState.GENERATING_OUTPUTS:
                if not output_directory.exists():
                    completed_at = datetime.now(UTC).isoformat()
                    write_outputs(
                        self.store,
                        request.job_id,
                        output_directory,
                        completed_at,
                        usb_ejected=False,
                    )
                self.store.transition(request.job_id, JobState.ARCHIVING)
                state = JobState.ARCHIVING
            if state == JobState.ARCHIVING:
                archive_staging(
                    self.settings.job_root / request.job_id,
                    self.fault_injector,
                )
                self.store.transition(request.job_id, JobState.EXPORTING)
            if completed_at is None:
                try:
                    completed_at = str(
                        json.loads(
                            (output_directory / "job_summary.json").read_text(encoding="utf-8")
                        )["completed_at"]
                    )
                except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
                    completed_at = None
            self._export(request, completed_at=completed_at)
        except Exception:  # noqa: BLE001 - worker boundary records terminal job failure
            self._fail(
                request,
                "archive_failed",
                "Job failed while generating or verifying the local archive.",
            )

    def _export(self, request: JobRequest, *, completed_at: str | None = None) -> None:
        try:
            self.store.transition(request.job_id, JobState.EXPORTING)
            device_id = self._device_id(request)
            self._job_usb_request(
                request,
                "export_results",
                device_id=device_id,
                selected_relative_path=request.source_relative_path,
                output_directory=str(self.settings.job_root / request.job_id / "outputs"),
            )
            if self.fault_injector is not None:
                self.fault_injector("export_after_publish", None)
            self._release_usb(request, device_id)
            self.store.transition(request.job_id, JobState.COMPLETED, completed_at)
        except UsbDeviceUnavailable:
            self.store.wait_for_usb(request.job_id)
        except UsbClientError:
            self._fail(request, "export_failed", "Job failed while exporting results to USB.")

    def run_forever(
        self,
        poll_seconds: float = 1,
        *,
        stop_event: ShutdownEvent | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            processed = self.run_once()
            active_request = self.store.active_request()
            if processed and active_request is None:
                return
            if not processed or (
                active_request is not None
                and self.store.job(active_request.job_id).state == JobState.WAITING_FOR_USB
            ):
                if stop_event is None:
                    time.sleep(poll_seconds)
                else:
                    stop_event.wait(poll_seconds)
