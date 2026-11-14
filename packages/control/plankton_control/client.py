from __future__ import annotations

import fcntl
import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, Protocol

# Bounded for the 100,000-item directory/import-manifest design limit.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
ProgressCallback = Callable[[int, int], None]
PreflightProgressCallback = Callable[[str, int, int | None], None]
ScanImportProgressCallback = Callable[[str, int, int, int, int, bool], None]


class UsbClientError(RuntimeError):
    pass


class UsbDeviceUnavailable(UsbClientError):
    pass


class UsbSecurityBlocked(UsbClientError):
    def __init__(self, relative_paths: list[str]) -> None:
        self.relative_paths = tuple(relative_paths)
        super().__init__("malware detected")


class UsbOperations(Protocol):
    def request(
        self,
        operation: str,
        *,
        progress: Callable[..., None] | None = None,
        **args: object,
    ) -> Any: ...


def _read_response(
    reader: BinaryIO,
    progress: Callable[..., None] | None,
) -> dict[str, object]:
    while True:
        response = reader.readline(MAX_RESPONSE_BYTES + 1)
        if not response:
            raise UsbClientError("USB host service returned invalid JSON")
        if len(response) > MAX_RESPONSE_BYTES:
            raise UsbClientError("USB host service response is too large")
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError as error:
            raise UsbClientError("USB host service returned invalid JSON") from error
        if isinstance(decoded, dict) and decoded.get("event") == "preflight_progress":
            stage = decoded.get("stage")
            processed = decoded.get("processed")
            total = decoded.get("total")
            if (
                not isinstance(stage, str)
                or stage not in {"discovering", "measuring"}
                or not isinstance(processed, int)
                or isinstance(processed, bool)
                or processed < 0
                or (total is not None and (not isinstance(total, int) or isinstance(total, bool)))
                or (total is not None and total < 0)
                or (total is not None and not 0 <= processed <= total)
            ):
                raise UsbClientError("USB host service returned invalid preflight progress")
            if progress is not None:
                progress(stage, processed, total)
            continue
        if isinstance(decoded, dict) and decoded.get("event") == "progress":
            processed = decoded.get("processed")
            total = decoded.get("total")
            if (
                not isinstance(processed, int)
                or isinstance(processed, bool)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or not 0 <= processed <= total
            ):
                raise UsbClientError("USB host service returned invalid progress")
            if progress is not None:
                progress(processed, total)
            continue
        if isinstance(decoded, dict) and decoded.get("event") == "scan_import_progress":
            stage = decoded.get("stage")
            processed = decoded.get("processed")
            total = decoded.get("total")
            processed_bytes = decoded.get("processed_bytes")
            total_bytes = decoded.get("total_bytes")
            heartbeat = decoded.get("heartbeat")
            if (
                stage not in {"scanning", "importing"}
                or not isinstance(processed, int)
                or isinstance(processed, bool)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or not 0 <= processed <= total
                or not isinstance(processed_bytes, int)
                or isinstance(processed_bytes, bool)
                or processed_bytes < 0
                or not isinstance(total_bytes, int)
                or isinstance(total_bytes, bool)
                or not 0 <= processed_bytes <= total_bytes
                or not isinstance(heartbeat, bool)
            ):
                raise UsbClientError("USB host service returned invalid scan/import progress")
            if progress is not None:
                progress(stage, processed, total, processed_bytes, total_bytes, heartbeat)
            continue
        if not isinstance(decoded, dict):
            raise UsbClientError("USB host service returned an invalid response")
        return decoded


class UnixUsbClient:
    def __init__(self, socket_path: Path, timeout_seconds: float = 3600) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        operation: str,
        *,
        progress: Callable[..., None] | None = None,
        **args: object,
    ) -> Any:
        payload = json.dumps({"operation": operation, "args": args}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout_seconds)
                connection.connect(str(self.socket_path))
                connection.sendall(payload)
                with connection.makefile("rb") as reader:
                    decoded = _read_response(reader, progress)
        except (OSError, TimeoutError) as error:
            raise UsbClientError("USB host service is unavailable") from error
        if not isinstance(decoded.get("ok"), bool):
            raise UsbClientError("USB host service returned an invalid response")
        if decoded["ok"]:
            return decoded.get("result")
        if decoded.get("error") == "device_unavailable":
            raise UsbDeviceUnavailable(str(decoded.get("message") or "USB device is unavailable"))
        if decoded.get("error") == "security_blocked":
            paths = decoded.get("relative_paths", [])
            raise UsbSecurityBlocked(
                [str(path) for path in paths] if isinstance(paths, list) else []
            )
        raise UsbClientError(str(decoded.get("message") or "USB host service request failed"))


def gpu_lock_available(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o660)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)
