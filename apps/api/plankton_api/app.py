from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from plankton_control import (
    UnixUsbClient,
    UsbClientError,
    UsbOperations,
    gpu_lock_available,
)
from plankton_core import JobState, JobStore, RuntimeMode
from pydantic import BaseModel, Field, field_validator
from starlette.responses import Response
from starlette.types import Scope

log = logging.getLogger("plankton-api")


class _WebFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response


@dataclass(frozen=True, slots=True)
class ApiSettings:
    database_path: Path
    job_root: Path
    usb_socket_path: Path
    gpu_lock_path: Path
    checkpoint_sha256: str
    model_name: str = "convnextv2_large.fcmae_ft_in22k_in1k"
    web_dist: Path | None = None

    @classmethod
    def from_environment(cls) -> ApiSettings:
        checkpoint_sha256 = os.environ.get("PLANKTON_CHECKPOINT_SHA256", "")
        if len(checkpoint_sha256) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in checkpoint_sha256
        ):
            raise RuntimeError("PLANKTON_CHECKPOINT_SHA256 must be a 64-character hex digest")
        web_dist = os.environ.get("PLANKTON_WEB_DIST")
        return cls(
            database_path=Path(
                os.environ.get("PLANKTON_DATABASE_PATH", "/var/lib/plankton-inference/app.db")
            ),
            job_root=Path(os.environ.get("PLANKTON_JOB_ROOT", "/var/lib/plankton-inference/jobs")),
            usb_socket_path=Path(
                os.environ.get(
                    "PLANKTON_USB_AGENT_SOCKET", "/run/plankton-inference/usb-agent.sock"
                )
            ),
            gpu_lock_path=Path(
                os.environ.get("PLANKTON_GPU_LOCK_PATH", "/run/plankton-inference/gpu.lock")
            ),
            checkpoint_sha256=checkpoint_sha256.lower(),
            model_name=os.environ.get(
                "PLANKTON_MODEL_NAME", "convnextv2_large.fcmae_ft_in22k_in1k"
            ),
            web_dist=Path(web_dist) if web_dist else None,
        )


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or not value:
        raise ValueError("path must be relative to the selected USB root")
    return path.as_posix()


class SourceSelection(BaseModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_relative_path: str = "."
    metadata_relative_path: str | None = None

    @field_validator("source_relative_path")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("metadata_relative_path")
    @classmethod
    def validate_metadata(cls, value: str | None) -> str | None:
        return _relative_path(value) if value is not None else None


class CreateJobRequest(SourceSelection):
    device_id: str = Field(min_length=1, max_length=128)
    preflight_id: str | None = Field(default=None, min_length=1, max_length=128)


class PreflightCancelled(RuntimeError):
    pass


class BrowserSessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


@dataclass
class _PreflightRun:
    preflight_id: str
    device_id: str
    source_relative_path: str
    metadata_relative_path: str | None
    status: str = "running"
    stage: str = "discovering"
    processed: int = 0
    total: int | None = None
    eta_seconds: int | None = None
    summary: dict[str, object] | None = None
    error: str | None = None
    cancel_requested: bool = False
    future: Future[object] | None = None
    last_processed: int = 0
    last_sample_at: float | None = None
    smoothed_rate: float | None = None
    completed_inline: bool = False
    transferred: bool = False
    consumed: bool = False
    handoff_token: str | None = None
    reconciliation_pending: bool = False
    reconciliation_in_progress: bool = False
    reconciled: bool = False


class _PreflightManager:
    def __init__(self, usb: UsbOperations) -> None:
        self.usb = usb
        self._runs: dict[str, _PreflightRun] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preflight")

    @staticmethod
    def _snapshot(run: _PreflightRun) -> dict[str, object]:
        return {
            "preflight_id": run.preflight_id,
            "status": run.status,
            "stage": run.stage,
            "processed": run.processed,
            "total": run.total,
            "percentage": (
                round(run.processed * 100 / run.total, 1)
                if run.total is not None and run.total > 0
                else None
            ),
            "eta_seconds": run.eta_seconds,
            "summary": run.summary,
            "error": run.error,
        }

    def _progress(self, run: _PreflightRun, stage: str, processed: int, total: int | None) -> None:
        now = time.monotonic()
        with self._lock:
            if run.cancel_requested:
                raise PreflightCancelled
            run.stage = stage
            run.processed = processed
            run.total = total
            if stage != "measuring" or total is None or processed == 0:
                run.eta_seconds = None
                run.last_processed = processed
                run.last_sample_at = now
                return
            if run.last_sample_at is not None and processed > run.last_processed:
                elapsed = now - run.last_sample_at
                if elapsed > 0:
                    instant_rate = (processed - run.last_processed) / elapsed
                    run.smoothed_rate = (
                        instant_rate
                        if run.smoothed_rate is None
                        else 0.25 * instant_rate + 0.75 * run.smoothed_rate
                    )
            run.last_processed = processed
            run.last_sample_at = now
            run.eta_seconds = (
                max(0, round((total - processed) / run.smoothed_rate))
                if run.smoothed_rate and run.smoothed_rate > 0
                else None
            )

    def _execute(self, run: _PreflightRun) -> None:
        try:
            result = self.usb.request(
                "preflight_selected_tree",
                progress=lambda stage, processed, total: self._progress(
                    run, stage, processed, total
                ),
                device_id=run.device_id,
                selected_relative_path=run.source_relative_path,
                metadata_relative_path=run.metadata_relative_path,
            )
            if not isinstance(result, dict):
                raise TypeError("storage preflight returned invalid data")
            try:
                fingerprint = self.usb.request(
                    "get_preflight_fingerprint",
                    device_id=run.device_id,
                    selected_relative_path=run.source_relative_path,
                    metadata_relative_path=run.metadata_relative_path,
                )
            except UsbClientError:
                fingerprint = None
            if isinstance(fingerprint, str) and fingerprint:
                result = dict(result)
                result["source_fingerprint"] = fingerprint
            with self._lock:
                if run.cancel_requested:
                    run.status = "cancelled"
                    run.stage = "cancelled"
                    run.eta_seconds = None
                else:
                    run.status = "completed"
                    run.stage = "completed"
                    run.processed = int(result.get("file_count", run.processed))
                    run.total = run.processed
                    run.eta_seconds = 0
                    run.summary = result
        except PreflightCancelled:
            with self._lock:
                run.status = "cancelled"
                run.stage = "cancelled"
                run.eta_seconds = None
        except Exception as error:  # noqa: BLE001 - public status records the failure
            with self._lock:
                run.eta_seconds = None
                run.summary = None
            try:
                self._release_run(run)
            except Exception as cleanup_error:  # noqa: BLE001 - retain reconciliation responsibility
                log.warning(
                    "preflight cleanup remains pending (%s)", type(cleanup_error).__name__
                )
            else:
                with self._lock:
                    run.status = "failed"
                    run.stage = "failed"
                    run.error = str(error)

    def _release_run(self, run: _PreflightRun, *, cancelled: bool = False) -> None:
        """Release ownership, committing the public cancellation only on success."""
        try:
            self.usb.request("release", device_id=run.device_id)
        except Exception:
            with self._lock:
                run.reconciliation_pending = True
                run.summary = None
                run.eta_seconds = None
                if run.status != "invalidated":
                    run.status = "failed"
                    run.stage = "failed"
                run.error = "USB cleanup is incomplete; retry release"
            raise
        with self._lock:
            run.reconciliation_pending = False
            run.reconciled = True
            run.summary = None
            run.eta_seconds = None
            if cancelled:
                run.status = "cancelled"
                run.stage = "cancelled"
                run.error = None

    def start(
        self,
        device_id: str,
        source_relative_path: str,
        metadata_relative_path: str | None,
    ) -> _PreflightRun:
        with self._lock:
            pending = [
                run.preflight_id for run in self._runs.values()
                if run.device_id == device_id and run.reconciliation_pending
                and run.status == "failed" and not run.transferred
            ]
        for preflight_id in pending:
            try:
                self.cancel(preflight_id)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "X-PICASSO-Preflight-ID": preflight_id}
                raise
        run = _PreflightRun(
            preflight_id=uuid4().hex,
            device_id=device_id,
            source_relative_path=source_relative_path,
            metadata_relative_path=metadata_relative_path,
        )
        with self._lock:
            if any(not existing.transferred and not existing.reconciled for existing in self._runs.values()):
                raise HTTPException(status_code=409, detail="Release the previous Preflight before starting another.")
            # Track the intent before claim: a failed reply may still leave a broker owner.
            self._runs[run.preflight_id] = run
        try:
            self.usb.request("claim", device_id=device_id)
            run.future = self._executor.submit(self._execute, run)
        except Exception as error:
            with self._lock:
                run.status = run.stage = "failed"
                run.error = str(error)
            try:
                self._release_run(run)
            except Exception as cleanup_error:
                log.warning("claim cleanup remains pending (%s)", type(cleanup_error).__name__)
                raise HTTPException(
                    status_code=503, detail="USB cleanup is incomplete; retry Preflight or Refresh",
                    headers={"X-PICASSO-Preflight-ID": run.preflight_id},
                ) from cleanup_error
            if isinstance(error, UsbClientError):
                raise HTTPException(
                    status_code=503, detail=str(error),
                    headers={"X-PICASSO-Preflight-ID": run.preflight_id},
                ) from error
            raise
        return run

    def get(self, preflight_id: str) -> _PreflightRun | None:
        with self._lock:
            return self._runs.get(preflight_id)

    def release_untransferred(self) -> None:
        with self._lock:
            pending = [run.preflight_id for run in self._runs.values() if not run.transferred and not run.reconciled]
        for preflight_id in pending:
            self.cancel(preflight_id)

    def cancel(self, preflight_id: str) -> dict[str, object]:
        with self._lock:
            run = self._runs.get(preflight_id)
            if run is None:
                raise KeyError(preflight_id)
            if run.status == "running":
                run.cancel_requested = True
            future = run.future
        if future is not None and not future.done():
            future.result()
        with self._lock:
            if run.reconciliation_in_progress:
                raise HTTPException(
                    status_code=503,
                    detail="USB cleanup is in progress; retry release",
                )
            needs_release = (
                not run.transferred
                and (run.reconciliation_pending or not run.reconciled)
            ) and run.status in {"cancelled", "failed", "completed", "invalidated"}
            if needs_release:
                run.reconciliation_in_progress = True
        if needs_release:
            try:
                self._release_run(run, cancelled=True)
            except Exception as error:
                raise HTTPException(
                    status_code=503,
                    detail="USB cleanup is incomplete; retry release",
                ) from error
            finally:
                with self._lock:
                    run.reconciliation_in_progress = False
        return self.snapshot(run)

    def mark_transferred(self, preflight_id: str, handoff_token: str) -> None:
        with self._lock:
            run = self._runs.get(preflight_id)
            if run is None or run.handoff_token != handoff_token:
                raise KeyError(preflight_id)
            run.transferred = True

    def invalidate_and_release(
        self, preflight_id: str, handoff_token: str, reason: str
    ) -> None:
        """Drop a preflight handoff and return its exclusive USB lease."""
        with self._lock:
            run = self._runs.get(preflight_id)
            if (
                run is None
                or run.transferred
                or run.status == "invalidated"
                or run.handoff_token != handoff_token
            ):
                return
            run.status = "invalidated"
            run.stage = "invalidated"
            run.error = reason
            run.summary = None
            run.reconciliation_in_progress = True
        try:
            self._release_run(run)
        except Exception as cleanup_error:  # noqa: BLE001 - retain reconciliation responsibility
            # Keep the preflight unusable while retaining host reconciliation responsibility.
            log.warning(
                "preflight handoff cleanup remains pending (%s)",
                type(cleanup_error).__name__,
            )
        finally:
            with self._lock:
                run.reconciliation_in_progress = False

    def snapshot(self, run: _PreflightRun) -> dict[str, object]:
        with self._lock:
            return self._snapshot(run)

    def consume(
        self,
        preflight_id: str,
        device_id: str,
        source_relative_path: str,
        metadata_relative_path: str | None,
    ) -> tuple[dict[str, object], str]:
        run = self.get(preflight_id)
        if run is None:
            raise HTTPException(status_code=409, detail="preflight is unavailable")
        with self._lock:
            if (
                run.status != "completed"
                or run.consumed
                or run.device_id != device_id
                or run.source_relative_path != source_relative_path
                or run.metadata_relative_path != metadata_relative_path
                or run.summary is None
            ):
                raise HTTPException(status_code=409, detail="preflight is not valid for this selection")
            expected_fingerprint = run.summary.get("source_fingerprint")
            handoff_token = uuid4().hex
            run.consumed = True
            run.handoff_token = handoff_token
            if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
                if run.completed_inline:
                    return dict(run.summary), handoff_token
                missing_fingerprint = True
            else:
                missing_fingerprint = False
        if missing_fingerprint:
            self.invalidate_and_release(
                preflight_id, handoff_token, "preflight source snapshot is unavailable"
            )
            raise HTTPException(status_code=409, detail="preflight source snapshot is unavailable")
        try:
            actual_fingerprint = self.usb.request(
                "fingerprint_selected_tree",
                device_id=device_id,
                selected_relative_path=source_relative_path,
                metadata_relative_path=metadata_relative_path,
            )
        except Exception as error:
            self.invalidate_and_release(
                preflight_id, handoff_token, "preflight source snapshot is unavailable"
            )
            raise HTTPException(status_code=409, detail="preflight source snapshot is unavailable") from error
        if actual_fingerprint != expected_fingerprint:
            self.invalidate_and_release(preflight_id, handoff_token, "preflight source changed")
            raise HTTPException(status_code=409, detail="preflight source changed")
        with self._lock:
            if run.status != "completed" or run.handoff_token != handoff_token:
                raise HTTPException(status_code=409, detail="preflight is unavailable")
            return dict(run.summary), handoff_token


class _BrowserSession:
    """One local window; the same lock fences close against durable job submission."""

    def __init__(self, preflights: _PreflightManager) -> None:
        self.preflights = preflights
        self.lock = threading.RLock()
        self.current: str | None = None
        self.closed: set[str] = set()
        self.last_seen = 0.0

    def check(self, session_id: str | None) -> None:
        # Existing non-GUI API clients remain compatible until a window owns the session.
        if session_id is None and self.current is None:
            return
        if session_id != self.current or session_id in self.closed:
            raise HTTPException(status_code=409, detail="Window session expired; reopen PICASSO.")

    def open(self, session_id: str) -> None:
        with self.lock:
            if session_id in self.closed:
                raise HTTPException(status_code=409, detail="Window session expired; reopen PICASSO.")
            if self.current != session_id and self.current is not None:
                self.closed.add(self.current)
            self.preflights.release_untransferred()
            self.current = session_id
            self.last_seen = time.monotonic()

    def close(self, session_id: str) -> None:
        with self.lock:
            self.closed.add(session_id)
            if self.current == session_id:
                self.preflights.release_untransferred()

    def heartbeat(self, session_id: str) -> None:
        with self.lock:
            self.check(session_id)
            self.last_seen = time.monotonic()

    def expire(self) -> None:
        with self.lock:
            if self.current is not None and time.monotonic() - self.last_seen >= 30:
                self.close(self.current)


def _job_payload(store: JobStore, job_id: str) -> dict[str, object]:
    try:
        job = store.job(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="job not found") from error
    progress = store.progress(job_id)
    failure = store.failure(job_id)
    percentage = (
        round(progress.processed_count * 100 / progress.total_count, 1)
        if progress.total_count
        else 0.0
    )
    return {
        "job_id": job.job_id,
        "state": job.state,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "stage": job.state,
        "counts": store.counts(job_id),
        "failure": (
            {
                "code": failure.code,
                "message": failure.message,
                "relative_paths": failure.relative_paths,
            }
            if failure is not None
            else None
        ),
        "progress": {
            "total": progress.total_count,
            "processed": progress.processed_count,
            "percentage": percentage,
            "images_per_second": progress.images_per_second,
            "eta_seconds": progress.eta_seconds,
            "stage_eta_seconds": progress.stage_eta_seconds,
            "job_eta_seconds": progress.job_eta_seconds,
            "total_bytes": progress.total_bytes,
            "processed_bytes": progress.processed_bytes,
            "throughput_bytes_per_second": progress.throughput_bytes_per_second,
            "progress_unit": progress.progress_unit,
            "updated_at": progress.updated_at,
        },
    }


def create_app(settings: ApiSettings, usb: UsbOperations | None = None) -> FastAPI:
    store = JobStore(settings.database_path)
    store.initialize()
    settings.job_root.mkdir(parents=True, exist_ok=True)
    usb = usb or UnixUsbClient(settings.usb_socket_path)
    preflights = _PreflightManager(usb)
    session = _BrowserSession(preflights)
    stop = threading.Event()

    def watch_session() -> None:
        while not stop.wait(1):
            try:
                session.expire()
            except Exception as error:  # noqa: BLE001 - retain exact cleanup for retry
                log.warning("Window session cleanup pending (%s)", type(error).__name__)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        watcher = threading.Thread(target=watch_session, name="browser-session", daemon=True)
        watcher.start()
        try:
            yield
        finally:
            stop.set()

    app = FastAPI(title="Plankton Inference Platform", version="0.1.0", lifespan=lifespan)

    @app.post("/api/v1/session/open")
    def open_session(request: BrowserSessionRequest) -> dict[str, object]:
        with session.lock:
            session.open(request.session_id)
            active = store.active_request()
            return {"session_id": request.session_id, "job": _job_payload(store, active.job_id) if active else None}

    @app.post("/api/v1/session/heartbeat")
    def heartbeat(request: BrowserSessionRequest) -> dict[str, bool]:
        session.heartbeat(request.session_id)
        return {"ok": True}

    @app.post("/api/v1/session/close")
    def close_session(request: BrowserSessionRequest) -> dict[str, bool]:
        session.close(request.session_id)
        return {"ok": True}

    @app.exception_handler(UsbClientError)
    async def usb_error(_: Request, error: UsbClientError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        usb.request("list_devices")
        return {"status": "ok"}

    @app.get("/api/v1/devices")
    def devices() -> object:
        return usb.request("list_devices")

    @app.get("/api/v1/devices/availability")
    def device_availability() -> dict[str, bool]:
        # Discovery populates the agent's sanitized availability flags. Keeping this
        # request after list_devices also supports test agents that override discovery.
        usb.request("list_devices")
        availability = usb.request("device_availability")
        if not isinstance(availability, dict) or not all(
            isinstance(key, str) and isinstance(value, bool)
            for key, value in availability.items()
        ):
            raise HTTPException(status_code=503, detail="USB device availability is invalid")
        return availability

    @app.get("/api/v1/devices/{device_id}/tree")
    def device_tree(
        device_id: str,
        path: Annotated[str, Query(max_length=4096)] = ".",
    ) -> object:
        try:
            relative_path = _relative_path(path)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return usb.request("list_directory", device_id=device_id, relative_path=relative_path)

    @app.post("/api/v1/devices/{device_id}/preflight")
    def preflight(device_id: str, selection: SourceSelection) -> object:
        with session.lock:
            session.check(selection.session_id)
            if store.active_request() is not None:
                raise HTTPException(status_code=409, detail="A job is already active.")
            result = start_preflight(device_id, selection)
            session.last_seen = time.monotonic()
            return result

    def start_preflight(device_id: str, selection: SourceSelection) -> object:
        run = preflights.start(
            device_id,
            selection.source_relative_path,
            selection.metadata_relative_path,
        )
        if run.future is not None:
            try:
                run.future.result(timeout=0.1)
                run.completed_inline = True
            except TimeoutError:
                pass
        snapshot = preflights.snapshot(run)
        if snapshot["status"] == "completed":
            return JSONResponse(status_code=200, content=snapshot)
        return JSONResponse(status_code=202, content=snapshot)

    @app.get("/api/v1/preflights/{preflight_id}")
    def preflight_status(preflight_id: str) -> dict[str, object]:
        run = preflights.get(preflight_id)
        if run is None:
            raise HTTPException(status_code=404, detail="preflight not found")
        return preflights.snapshot(run)

    @app.delete("/api/v1/preflights/{preflight_id}")
    def cancel_preflight(preflight_id: str) -> dict[str, object]:
        try:
            with session.lock:
                return preflights.cancel(preflight_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="preflight not found") from error

    @app.post("/api/v1/jobs", status_code=202)
    def create_job(request: CreateJobRequest) -> dict[str, object]:
        run = preflights.get(request.preflight_id) if request.preflight_id else None
        if run is not None and run.consumed:
            raise HTTPException(status_code=409, detail="preflight is not valid for this selection")
        with session.lock:
            session.check(request.session_id)
            return submit_job(request)

    def submit_job(request: CreateJobRequest) -> dict[str, object]:
        if not request.preflight_id:
            raise HTTPException(status_code=409, detail="completed preflight is required")
        handoff_token: str | None = None
        try:
            storage, handoff_token = preflights.consume(
                request.preflight_id,
                request.device_id,
                request.source_relative_path,
                request.metadata_relative_path,
            )
            required_free = storage.get("required_free_bytes")
            if isinstance(required_free, int):
                free = shutil.disk_usage(settings.job_root.resolve()).free
                if free < required_free:
                    raise HTTPException(status_code=507, detail="insufficient local storage for import")
            if not isinstance(storage, dict) or not isinstance(storage.get("has_enough_space"), bool):
                raise HTTPException(status_code=503, detail="storage preflight returned invalid data")
            if not storage["has_enough_space"]:
                raise HTTPException(status_code=507, detail="insufficient local storage for import")
            devices = usb.request("list_devices")
            if not isinstance(devices, list):
                raise HTTPException(status_code=503, detail="USB device list is invalid")
            matches = [
                device
                for device in devices
                if isinstance(device, dict) and device.get("device_id") == request.device_id
            ]
            if len(matches) != 1:
                raise HTTPException(status_code=503, detail="USB device identity is ambiguous")
            device_uuid = matches[0].get("uuid")
            device_serial = matches[0].get("serial")
            if not isinstance(device_uuid, str) or not device_uuid:
                raise HTTPException(status_code=503, detail="USB device has no filesystem UUID")
            if not isinstance(device_serial, str):
                raise HTTPException(status_code=503, detail="USB device identity is invalid")
            job_id = str(uuid4())
            store.create_requested_job(
                job_id,
                settings.model_name,
                settings.checkpoint_sha256,
                request.device_id,
                request.source_relative_path,
                request.metadata_relative_path,
                device_uuid=device_uuid,
                device_serial=device_serial,
                safe_eject=False,
            )
        except RuntimeError as error:
            if handoff_token is not None:
                preflights.invalidate_and_release(
                    request.preflight_id, handoff_token, "job ownership handoff failed"
                )
            raise HTTPException(status_code=409, detail=str(error)) from error
        except HTTPException:
            if handoff_token is not None:
                preflights.invalidate_and_release(
                    request.preflight_id, handoff_token, "job ownership handoff failed"
                )
            raise
        except Exception:
            if handoff_token is not None:
                preflights.invalidate_and_release(
                    request.preflight_id, handoff_token, "job ownership handoff failed"
                )
            raise
        assert handoff_token is not None
        preflights.mark_transferred(request.preflight_id, handoff_token)
        return _job_payload(store, job_id)

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: str) -> dict[str, object]:
        return _job_payload(store, job_id)

    @app.get("/api/v1/jobs/{job_id}/summary")
    def summary(job_id: str) -> object:
        try:
            record = store.job(job_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="job not found") from error
        if record.state != JobState.COMPLETED:
            raise HTTPException(status_code=409, detail="job is not completed")
        summary_path = settings.job_root / job_id / "outputs" / "job_summary.json"
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=500, detail="job summary is unavailable") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=500, detail="job summary is invalid")
        return {
            **payload,
            "archive_path": f"jobs/{job_id}/archive/source_images.zip",
            "usb_ejected": False,
        }

    @app.get("/api/v1/runtime")
    def runtime() -> dict[str, object]:
        lock_available = gpu_lock_available(settings.gpu_lock_path)
        admin_mode = store.runtime_mode()
        active = store.active_request()
        inference_running = (
            active is not None and store.job(active.job_id).state == JobState.INFERENCING
        )
        if admin_mode == RuntimeMode.TRAINING:
            mode, status, source = RuntimeMode.TRAINING, "admin_paused", "administrator"
        elif inference_running:
            mode, status, source = (
                RuntimeMode.INFERENCE_ENABLED,
                "inference_running",
                "platform",
            )
        elif not lock_available:
            mode, status, source = RuntimeMode.TRAINING, "training", "training_entrypoint"
        else:
            mode, status, source = RuntimeMode.INFERENCE_ENABLED, "ready", "platform"
        return {
            "mode": mode,
            "status": status,
            "source": source,
            "gpu_lock_available": lock_available,
        }

    if settings.web_dist is not None and settings.web_dist.is_dir():
        app.mount("/", _WebFiles(directory=settings.web_dist, html=True), name="web")
    return app
