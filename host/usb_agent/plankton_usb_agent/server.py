from __future__ import annotations

import json
import os
import socketserver
import threading
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .agent import DeviceUnavailable, ImportManifest, SecurityBlocked, UsbAgent, UsbAgentError

MAX_REQUEST_BYTES = 64 * 1024


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def dispatch(
    agent: UsbAgent,
    request: object,
    progress: Callable[..., None] | None = None,
) -> object:
    if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
        raise UsbAgentError("request must contain an operation")
    args = request.get("args", {})
    if not isinstance(args, dict):
        raise UsbAgentError("request args must be an object")
    if any(key in args for key in ("progress", "skip_scan", "scan_receipt", "receipt")):
        raise UsbAgentError("unsupported argument")
    operation = request["operation"]
    methods: dict[str, Callable[..., object]] = {
        "list_devices": agent.list_devices,
        "has_unsupported_removable_media": agent.has_unsupported_removable_media,
        "device_availability": agent.device_availability,
        "claim": agent.claim,
        "list_directory": agent.list_directory,
        # Read-only compatibility probe; copy never consumes its result.
        "scan_selected_tree": agent.scan_selected_tree,
        "scan_and_import": agent.scan_and_import,
        # Compatibility spelling; it still routes through the atomic operation.
        "copy_to_staging": agent.scan_and_import,
        "preflight_selected_tree": agent.preflight_selected_tree,
        "get_preflight_fingerprint": agent.get_preflight_fingerprint,
        "fingerprint_selected_tree": agent.fingerprint_selected_tree,
        "export_results": agent.export_results,
        "flush": agent.flush,
        "release": agent.release,
        "eject": agent.eject,
    }
    method = methods.get(operation)
    if method is None:
        raise UsbAgentError("unsupported operation")
    if "staging_directory" in args:
        args["staging_directory"] = Path(args["staging_directory"])
    if "output_directory" in args:
        args["output_directory"] = Path(args["output_directory"])
    agent.journal_operation(operation, "started")
    try:
        if operation == "preflight_selected_tree":
            agent._progress_local.preflight = progress
        try:
            progress_arg: dict[str, object] = {}
            if operation in {"scan_and_import", "copy_to_staging"}:
                if operation == "copy_to_staging" and progress is not None:
                    progress_arg["progress"] = lambda stage, processed, total, *_rest: (
                        progress(processed, total) if stage == "importing" else None
                    )
                else:
                    progress_arg["progress"] = progress
            result = method(**args, **progress_arg)
        finally:
            if operation == "preflight_selected_tree":
                agent._progress_local.preflight = None
    except Exception as error:
        agent.journal_operation(operation, "failed", type(error).__name__)
        raise
    agent.journal_operation(operation, "completed")
    if isinstance(result, ImportManifest):
        return {"entry_count": len(result.entries)}
    return _jsonable(result)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        write_lock = threading.Lock()
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(line) > MAX_REQUEST_BYTES:
            response: dict[str, Any] = {"ok": False, "error": "request too large"}
        else:
            try:
                request = json.loads(line)

                def send_progress(*values: object) -> None:
                    if len(values) == 2:
                        processed, total = values
                        event = {"event": "progress", "processed": processed, "total": total}
                    elif len(values) == 3:
                        stage, processed, total = values
                        event = {
                            "event": "preflight_progress",
                            "stage": stage,
                            "processed": processed,
                            "total": total,
                        }
                    elif len(values) == 6:
                        stage, processed, total, processed_bytes, total_bytes, heartbeat = values
                        event = {
                            "event": "scan_import_progress",
                            "stage": stage,
                            "processed": processed,
                            "total": total,
                            "processed_bytes": processed_bytes,
                            "total_bytes": total_bytes,
                            "heartbeat": heartbeat,
                        }
                    else:
                        raise TypeError("invalid progress event")
                    with write_lock:
                        self.wfile.write(json.dumps(event).encode() + b"\n")
                        self.wfile.flush()

                response = {
                    "ok": True,
                    "result": dispatch(
                        self.server.agent,  # type: ignore[attr-defined]
                        request,
                        send_progress,
                    ),
                }
            except SecurityBlocked as error:
                response = {
                    "ok": False,
                    "error": "security_blocked",
                    "relative_paths": list(error.relative_paths),
                }
            except DeviceUnavailable as error:
                response = {
                    "ok": False,
                    "error": "device_unavailable",
                    "message": str(error),
                }
            except (UsbAgentError, TypeError, ValueError, json.JSONDecodeError) as error:
                response = {"ok": False, "error": "usb_agent_error", "message": str(error)}
            except PermissionError:
                response = {
                    "ok": False,
                    "error": "host_filesystem_permission_denied",
                    "message": "host filesystem permission denied",
                }
            except OSError:
                response = {"ok": False, "error": "host filesystem operation failed"}
        with write_lock:
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()


class UsbAgentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    agent: UsbAgent

    def __init__(self, socket_path: Path, agent: UsbAgent) -> None:
        super().__init__(str(socket_path), _Handler)
        self.agent = agent


def serve(socket_path: Path, agent: UsbAgent) -> None:
    agent.reconcile_persisted_ownership()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        if not socket_path.is_socket():
            raise UsbAgentError("refusing to replace non-socket path")
        socket_path.unlink()
    try:
        with UsbAgentServer(socket_path, agent) as server:
            os.chmod(socket_path, 0o660)
            server.serve_forever()
    finally:
        agent.release_all()
        if socket_path.is_socket():
            socket_path.unlink()
