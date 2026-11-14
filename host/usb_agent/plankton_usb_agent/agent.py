from __future__ import annotations

import array
import ast
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

from .ownership import OwnershipError, OwnershipLease, OwnershipUnavailable, UsbOwnership

ALLOWED_FILESYSTEMS = {"exfat": "exfat", "ntfs": "ntfs", "ntfs3": "ntfs", "ext4": "ext4"}
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
OUTPUT_FILES = (
    "predictions_top1.csv",
    "low_confidence_top3.csv",
    "rejected_images.csv",
    "job_summary.json",
    "counts_summary.csv",
)
LEGACY_OUTPUT_FILES = (*OUTPUT_FILES[:-1], "README.md")
RESULT_DIRECTORY = re.compile(r"^inference_results_(\d{3})$")
TRANSIENT_MOUNT_BUSY = "already mounted or mount point busy"
MOUNT_RETRY_LIMIT = 3
MOUNT_RETRY_DELAY_SECONDS = 0.1
RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
MountBrokerRequest = Callable[[dict[str, str]], dict[str, object]]
ProgressCallback = Callable[[int, int], None]
ScanImportProgressCallback = Callable[[str, int, int, int, int, bool], None]
PreflightProgressCallback = Callable[[str, int, int | None], None]
SAFE_BROKER_ERRORS = frozenset(
    {
        "native mount is still present",
        "native mount is busy",
        "owned mount is unavailable",
        "owned mount has unsafe state",
        "exclusive ownership postcondition failed",
        "device is already owned",
        "stale ownership lease",
        "unauthorized mount broker peer",
        "native USB view was not restored",
        "controlled mount did not release",
        "device is still mounted",
        "device remained mounted",
        "USB ownership recovery is pending",
    }
)


class UsbAgentError(RuntimeError):
    pass


class DeviceUnavailable(UsbAgentError):
    pass


class ScannerUnavailable(UsbAgentError):
    pass


class SecurityBlocked(UsbAgentError):
    def __init__(self, relative_paths: Sequence[str]) -> None:
        self.relative_paths = tuple(relative_paths)
        super().__init__("malware detected: " + ", ".join(self.relative_paths))


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    block_device: str
    drive_device: str
    filesystem: str
    uuid: str
    serial: str
    label: str | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    relative_path: str
    kind: str
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ImportManifest:
    schema_version: str
    device_id: str
    device_uuid: str
    device_serial: str
    selected_relative_path: str
    created_at: str
    entries: tuple[ManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class PreflightSummary:
    file_count: int
    source_bytes: int
    required_free_bytes: int
    local_free_bytes: int
    has_enough_space: bool


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        check=False,
        text=True,
        timeout=3600,
    )


def _truthy(value: object) -> bool:
    return value is True or str(value).lower() in {"1", "true", "yes"}


def _canonical_filesystem(fstype: object, fsver: object) -> str | None:
    filesystem = str(fstype or "").lower()
    if filesystem == "vfat":
        return {"FAT16": "fat16", "FAT32": "fat32"}.get(str(fsver or "").upper())
    return ALLOWED_FILESYSTEMS.get(filesystem)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_image_candidate(path: Path) -> bool:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            signature = handle.read(len(PNG_SIGNATURE))
    except OSError:
        return False
    return signature.startswith((PNG_SIGNATURE, JPEG_SIGNATURE))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class UsbAgent:
    def __init__(
        self,
        *,
        job_root: Path = Path("/var/lib/plankton-inference/jobs"),
        signature_directory: Path = Path("/var/lib/clamav"),
        max_signature_age_hours: int = 72,
        reserve_bytes: int = 20 * 1024**3,
        copy_progress_images: int = 1000,
        run_command: RunCommand = _run,
        mount_broker_request: MountBrokerRequest | None = None,
        mount_broker_socket: Path = Path("/run/plankton-inference/usb-mount-broker.sock"),
        ownership_root_parent: Path | None = Path("/media/plankton-usb"),
    ) -> None:
        if copy_progress_images < 1:
            raise ValueError("copy progress interval must be positive")
        self.job_root = job_root
        self.signature_directory = signature_directory
        self.max_signature_age_hours = max_signature_age_hours
        self.reserve_bytes = reserve_bytes
        self.copy_progress_images = copy_progress_images
        self.run_command = run_command
        self._live_broker = mount_broker_request is None
        self.mount_broker_request = mount_broker_request or self._request_mount_broker
        self.mount_broker_socket = mount_broker_socket
        self._progress_local = threading.local()
        self._scan_import_lock = threading.Lock()
        self._serialless_uuids: set[str] = set()
        self._device_availability = {"unsupported_removable_media_detected": False}
        self._preflight_fingerprints: dict[tuple[str, str, str | None], str] = {}
        self._ownership_leases: dict[str, OwnershipLease] = {}
        self._ownership = UsbOwnership(
            self._ownership_broker_call,
            self._verify_owned_view,
            verify_released_view=self._verify_released_view,
            owned_root_parent=ownership_root_parent,
        )

    def _request_mount_broker(self, payload: dict[str, str]) -> dict[str, object]:
        request = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(30)
                connection.connect(str(self.mount_broker_socket))
                namespace_fd = os.open("/proc/self/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
                try:
                    sent = connection.sendmsg(
                        [request],
                        [
                            (
                                socket.SOL_SOCKET,
                                socket.SCM_RIGHTS,
                                array.array("i", [namespace_fd]),
                            )
                        ],
                    )
                    if sent != len(request):
                        raise UsbAgentError("mount broker request was truncated")
                finally:
                    os.close(namespace_fd)
                response = b""
                while not response.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    response += chunk
                    if len(response) > 65536:
                        raise UsbAgentError("mount broker response is too large")
        except (OSError, TimeoutError) as error:
            raise UsbAgentError("USB mount broker is unavailable") from error
        try:
            decoded = json.loads(response.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UsbAgentError("invalid USB mount broker response") from error
        if not isinstance(decoded, dict):
            raise UsbAgentError("invalid USB mount broker response")
        return cast(dict[str, object], decoded)

    def _mount_broker(
        self, operation: str, device: Device, **values: Path | str
    ) -> dict[str, object]:
        payload = {
            "operation": operation,
            "block_device": device.block_device,
            "drive_device": device.drive_device,
            "uuid": device.uuid,
            "serial": device.serial,
            "owned_root": "",
            **{name: str(value) for name, value in values.items()},
        }
        result = self.mount_broker_request(payload)
        if result.get("ok") is not True:
            detail = str(result.get("error") or "mount broker rejected request")
            raise UsbAgentError(
                detail if detail in SAFE_BROKER_ERRORS else "mount broker operation failed"
            )
        return result

    def _ownership_broker_call(
        self, operation: str, device: Device, **values: Path | str
    ) -> dict[str, object]:
        return self._mount_broker(operation, device, **values)

    def _verify_owned_view(self, device: Device, lease: OwnershipLease) -> bool:
        if self._live_broker and lease.owned_root.parent != Path("/media/plankton-usb"):
            return False
        mounts = self._mount_infos(device, service_target=lease.owned_root)
        owned = next(((target, options) for target, options in mounts if target == lease.owned_root), None)
        return (
            owned is not None
            and "rw" in owned[1]
            and "ro" not in owned[1]
            and {"nodev", "nosuid", "noexec"}.issubset(owned[1])
        )

    def _verify_released_view(self, device: Device, lease: OwnershipLease) -> bool:
        """Wait until this namespace has consumed the broker's release event."""
        mounts = self._mount_infos(device, service_target=lease.owned_root)
        return not any(target == lease.owned_root for target, _ in mounts)

    def _remember_ownership(self, device: Device, lease: OwnershipLease) -> None:
        self._ownership_leases[device.device_id] = lease

    def journal_operation(self, operation: str, status: str, error: str | None = None) -> None:
        del operation, status, error

    def release_all(self) -> None:
        """Release every owned lease before the host service exits."""
        for device_id in tuple(self._ownership_leases):
            try:
                self.release(device_id)
            except UsbAgentError as error:
                self.journal_operation("release", "failed", type(error).__name__)

    def reconcile_persisted_ownership(self) -> None:
        """Reconcile broker leases before the service accepts public requests."""
        failures: list[str] = []
        for device in self.list_devices():
            try:
                self._ownership.reconcile(device)
            except (OwnershipError, UsbAgentError) as error:
                failures.append(device.device_id)
                self.journal_operation("reconcile", "failed", type(error).__name__)
        if failures:
            raise UsbAgentError("USB ownership reconciliation failed")

    def _command(
        self, argv: Sequence[str], *, allowed_return_codes: frozenset[int] = frozenset({0})
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.run_command(argv)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
            raise UsbAgentError(f"command unavailable: {argv[0]}") from error
        if result.returncode not in allowed_return_codes:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise UsbAgentError(f"{argv[0]} failed: {detail}")
        return result

    def _discover_devices(self) -> tuple[tuple[Device, ...], bool, bool]:
        result = self._command(
            (
                "lsblk",
                "--json",
                "--bytes",
                "--output",
                "PATH,PKNAME,TYPE,RM,TRAN,FSTYPE,FSVER,UUID,SERIAL,LABEL,SIZE",
            )
        )
        try:
            blocks = json.loads(result.stdout)["blockdevices"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise UsbAgentError("invalid lsblk response") from error

        if not isinstance(blocks, list):
            raise UsbAgentError("invalid lsblk device list")
        nodes: dict[str, dict[str, Any]] = {}
        parents: dict[str, str] = {}

        def collect(items: Sequence[dict[str, Any]], parent_path: str = "") -> None:
            for node in items:
                node_path = str(node.get("path") or "")
                if not node_path:
                    continue
                nodes[node_path] = node
                parent_name = str(node.get("pkname") or "")
                parents[node_path] = (
                    parent_name if parent_name.startswith("/dev/") else f"/dev/{parent_name}"
                ) if parent_name else parent_path
                children = node.get("children") or []
                if isinstance(children, list):
                    collect(children, node_path)

        collect(blocks)

        def context(node_path: str, seen: frozenset[str] = frozenset()) -> tuple[bool, str, str, str]:
            if node_path in seen:
                return False, "", "", ""
            node = nodes[node_path]
            parent_path = parents.get(node_path, "")
            inherited = context(parent_path, seen | {node_path}) if parent_path in nodes else (False, "", "", "")
            removable, transport, serial, drive = inherited
            return (
                removable or _truthy(node.get("rm")),
                str(node.get("tran") or transport or ""),
                str(node.get("serial") or serial or ""),
                node_path if str(node.get("type") or "") == "disk" else drive,
            )

        devices: list[Device] = []
        unsupported_removable_media_detected = False
        missing_filesystem_uuid_detected = False
        for node_path, node in nodes.items():
            removable, transport, serial, drive = context(node_path)
            filesystem = _canonical_filesystem(node.get("fstype"), node.get("fsver"))
            uuid = str(node.get("uuid") or "")
            if (removable or transport == "usb") and filesystem and not uuid:
                missing_filesystem_uuid_detected = True
            if (
                (removable or transport == "usb")
                and node.get("fstype")
                and filesystem is None
            ):
                unsupported_removable_media_detected = True
            if not (filesystem and uuid and (removable or transport == "usb")):
                continue
            if not serial:
                self._serialless_uuids.add(uuid)
            identity_serial = "" if uuid in self._serialless_uuids else serial
            identity = hashlib.sha256(f"{uuid}\0{identity_serial}".encode()).hexdigest()[:24]
            devices.append(
                Device(
                    device_id=identity,
                    block_device=node_path,
                    drive_device=drive or node_path,
                    filesystem=filesystem,
                    uuid=uuid,
                    serial=serial,
                    label=str(node["label"]) if node.get("label") else None,
                    size_bytes=int(node.get("size") or 0),
                )
            )
        self._device_availability = {"unsupported_removable_media_detected": unsupported_removable_media_detected}
        if missing_filesystem_uuid_detected:
            self._device_availability["missing_filesystem_uuid_detected"] = True
        return (
            tuple(sorted(devices, key=lambda device: device.device_id)),
            unsupported_removable_media_detected,
            missing_filesystem_uuid_detected,
        )

    def list_devices(self) -> tuple[Device, ...]:
        return self._discover_devices()[0]

    def has_unsupported_removable_media(self) -> bool:
        return self._discover_devices()[1]

    def device_availability(self) -> dict[str, bool]:
        return dict(self._device_availability)

    def _device(self, device_id: str) -> Device:
        matches = [device for device in self.list_devices() if device.device_id == device_id]
        if len(matches) != 1:
            raise DeviceUnavailable("USB device is absent or identity is ambiguous")
        return matches[0]

    def _mount_infos(
        self, device: Device, *, service_target: Path | None = None
    ) -> tuple[tuple[Path, frozenset[str]], ...]:
        result = self._command(
            ("findmnt", "--json", "--source", device.block_device, "--output", "TARGET,OPTIONS"),
            allowed_return_codes=frozenset({0, 1}),
        )
        mounts: tuple[tuple[Path, frozenset[str]], ...] = ()
        if result.returncode == 1:
            pass
        else:
            try:
                filesystems = json.loads(result.stdout)["filesystems"]
                mounts = tuple(
                    (Path(str(filesystem["target"])), frozenset(str(filesystem["options"]).split(",")))
                    for filesystem in filesystems
                )
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise UsbAgentError("invalid findmnt response") from error
        if any(not target.is_absolute() for target, _ in mounts):
            raise UsbAgentError("invalid findmnt response")
        if service_target is not None and not any(target == service_target for target, _ in mounts):
            target_result = self._command(
                (
                    "findmnt",
                    "--json",
                    "--target",
                    str(service_target),
                    "--output",
                    "SOURCE,TARGET,OPTIONS",
                ),
                allowed_return_codes=frozenset({0, 1}),
            )
            if target_result.returncode == 0:
                try:
                    target_filesystems = json.loads(target_result.stdout)["filesystems"]
                    target_mounts: tuple[tuple[Path, frozenset[str]], ...] = tuple(
                        (
                            Path(str(filesystem["target"])),
                            frozenset(str(filesystem["options"]).split(",")),
                        )
                        for filesystem in target_filesystems
                        if (
                            str(filesystem.get("source") or "") == device.block_device
                            or str(filesystem.get("source") or "").startswith(
                                device.block_device + "["
                            )
                        )
                    )
                except (json.JSONDecodeError, KeyError, TypeError) as error:
                    raise UsbAgentError("invalid findmnt response") from error
                mounts += tuple(
                    (target, options)
                    for target, options in target_mounts
                    if target == service_target
                )
        return mounts

    def _mount_info(self, device: Device) -> tuple[Path, frozenset[str]] | None:
        mounts = self._mount_infos(device)
        return mounts[0] if mounts else None

    @staticmethod
    def _validate_mount_options(
        options: frozenset[str], *, writable: bool, require_noexec: bool = True
    ) -> None:
        required = {"nodev", "nosuid", "rw" if writable else "ro"}
        if require_noexec:
            required.add("noexec")
        missing = required - options
        if missing:
            raise UsbAgentError("unsafe mount options; missing " + ",".join(sorted(missing)))

    @staticmethod
    def _mount_for_mode(
        mounts: Sequence[tuple[Path, frozenset[str]]],
        *,
        writable: bool,
        require_noexec: bool = True,
    ) -> tuple[Path, frozenset[str]] | None:
        for target, options in mounts:
            try:
                UsbAgent._validate_mount_options(
                    options, writable=writable, require_noexec=require_noexec
                )
            except UsbAgentError:
                continue
            return target, options
        return None

    @staticmethod
    def _mount_for_user(
        mounts: Sequence[tuple[Path, frozenset[str]]], user: str
    ) -> tuple[Path, frozenset[str]] | None:
        for target, options in mounts:
            parts = target.parts
            target_user = (
                parts[3]
                if len(parts) >= 4 and parts[:3] == ("/", "run", "media")
                else parts[2]
                if len(parts) >= 3 and parts[:2] == ("/", "media")
                else ""
            )
            if target_user == user:
                return target, options
        return None

    def _unmount(self, device: Device) -> None:
        self._command(
            (
                "udisksctl",
                "unmount",
                "--block-device",
                device.block_device,
                "--no-user-interaction",
            )
        )

    @staticmethod
    def _udisks_block_object_path(block_device: str) -> str:
        name = Path(block_device).name
        encoded = "".join(
            char
            if char.isascii() and char.isalnum()
            else "_5f"
            if char == "_"
            else f"_{ord(char):02x}"
            for char in name
        )
        return f"/org/freedesktop/UDisks2/block_devices/{encoded}"

    def _mount_as_user(self, device: Device, user: str, *, writable: bool = True) -> Path:
        result = self._command(
            (
                "/usr/bin/gdbus",
                "call",
                "--system",
                "--dest",
                "org.freedesktop.UDisks2",
                "--object-path",
                self._udisks_block_object_path(device.block_device),
                "--method",
                "org.freedesktop.UDisks2.Filesystem.Mount",
                f"{{'options': <'{('rw' if writable else 'ro')},nodev,nosuid,noexec'>, 'as-user': <'{user}'>}}",
            )
        )
        try:
            value = ast.literal_eval(result.stdout.strip())
            target = Path(str(value[0]))
        except (SyntaxError, ValueError, IndexError, TypeError) as error:
            raise UsbAgentError("invalid UDisks mount response") from error
        if not target.is_absolute():
            raise UsbAgentError("invalid UDisks mount response")
        if target.parts[:3] == ("/", "run", "media"):
            valid = len(target.parts) >= 4 and target.parts[3] == user
        else:
            valid = (
                len(target.parts) >= 3
                and target.parts[:2] == ("/", "media")
                and target.parts[2] == user
            )
        if not valid:
            raise UsbAgentError("invalid UDisks mount response")
        return target

    def _close_nautilus(self) -> None:
        self._command(("/usr/bin/nautilus", "--quit"))

    def claim(self, device_id: str) -> str:
        device = self._device(device_id)
        try:
            lease = self._ownership.claim(device)
        except OwnershipError as error:
            if str(error) != "native mount is busy":
                raise UsbAgentError(str(error)) from error
            if self._live_broker:
                raise UsbAgentError(str(error)) from error
            self._close_nautilus()
            try:
                lease = self._ownership.claim(device)
            except OwnershipError as retry_error:
                raise UsbAgentError(str(retry_error)) from retry_error
        self._remember_ownership(device, lease)
        return str(lease.owned_root)

    @staticmethod
    def _contained_path(
        root: Path,
        relative_path: str,
        *,
        must_exist: bool = True,
        directory: bool | None = None,
    ) -> Path:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise UsbAgentError("path must be relative to the mount root")
        root = root.resolve(strict=True)
        current = root
        for part in relative.parts:
            if part in {"", "."}:
                continue
            current = current / part
            if current.is_symlink():
                raise UsbAgentError("symbolic links are not allowed")
        try:
            resolved = current.resolve(strict=must_exist)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            raise UsbAgentError("path is outside the permitted root or does not exist") from error
        if directory is True and not resolved.is_dir():
            raise UsbAgentError("path is not a directory")
        if directory is False and not resolved.is_file():
            raise UsbAgentError("path is not a regular file")
        return resolved

    def _mounted_root(
        self, device: Device, *, writable: bool, native_browse: bool = False
    ) -> Path:
        lease = self._ownership_leases.get(device.device_id)
        if lease is not None:
            mounts = self._mount_infos(device, service_target=lease.owned_root)
            owned = next(
                ((target, options) for target, options in mounts if target == lease.owned_root),
                None,
            )
            if owned is None or (writable and "rw" not in owned[1]):
                raise UsbAgentError("USB exclusive lease is unavailable")
            return owned[0]
        mounts = self._mount_infos(device)
        mounted = self._mount_for_mode(
            mounts, writable=writable, require_noexec=not native_browse
        )
        if not writable and mounted is None:
            mounted = self._mount_for_mode(
                mounts, writable=True, require_noexec=not native_browse
            )
        if mounted is None:
            raise UsbAgentError("USB is not mounted")
        return mounted[0]

    def list_directory(
        self, device_id: str, relative_path: str = "."
    ) -> tuple[DirectoryEntry, ...]:
        device = self._device(device_id)
        if device.device_id in self._ownership_leases:
            raise UsbAgentError("USB is exclusively owned by PICASSO")
        root = self._mounted_root(device, writable=False, native_browse=True).resolve(strict=True)
        directory = self._contained_path(root, relative_path, directory=True)
        entries: list[DirectoryEntry] = []
        with os.scandir(directory) as children:
            for child in children:
                if child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False):
                    kind, size = "directory", None
                elif child.is_file(follow_symlinks=False):
                    kind, size = "file", child.stat(follow_symlinks=False).st_size
                else:
                    continue
                relative = Path(child.path).relative_to(root).as_posix()
                entries.append(DirectoryEntry(child.name, relative, kind, size))
        return tuple(
            sorted(entries, key=lambda entry: (entry.kind != "directory", entry.name.casefold()))
        )

    def _validate_signatures(self) -> None:
        signatures = [
            path
            for pattern in ("*.cvd", "*.cld")
            for path in self.signature_directory.glob(pattern)
            if path.is_file()
        ]
        if not signatures:
            raise ScannerUnavailable("ClamAV signatures are unavailable")
        daily = [path for path in signatures if path.stem == "daily"]
        newest = max(path.stat().st_mtime for path in (daily or signatures))
        age_hours = (datetime.now(UTC).timestamp() - newest) / 3600
        if age_hours > self.max_signature_age_hours:
            raise ScannerUnavailable(f"ClamAV signatures are stale ({age_hours:.1f} hours)")

    def scan_selected_tree(self, device_id: str, relative_path: str) -> None:
        device = self._device(device_id)
        root = self._mounted_root(device, writable=False).resolve(strict=True)
        selected = self._contained_path(root, relative_path, directory=True)
        self._validate_signatures()
        try:
            result = self.run_command(
                (
                    "clamdscan",
                    "--stream",
                    "--infected",
                    "--no-summary",
                    "--",
                    str(selected),
                )
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
            raise ScannerUnavailable("ClamAV scanner is unavailable") from error
        if result.returncode == 0:
            return
        if result.returncode == 1:
            threats: list[str] = []
            for line in result.stdout.splitlines():
                if not line.endswith(" FOUND"):
                    continue
                raw_path = line.rsplit(": ", 1)[0]
                try:
                    threats.append(Path(raw_path).relative_to(root).as_posix())
                except ValueError:
                    threats.append("<scanner-reported-threat>")
            raise SecurityBlocked(threats or ("<scanner-reported-threat>",))
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ScannerUnavailable(f"ClamAV scan failed: {detail}")

    @staticmethod
    def _walk_import_files(selected: Path, metadata: Path | None) -> Iterator[Path]:
        stack = [selected]
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as children:
                entries = sorted(children, key=lambda entry: entry.name.casefold(), reverse=True)
            for entry in entries:
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if RESULT_DIRECTORY.fullmatch(entry.name):
                        continue
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False) and (
                    _is_image_candidate(path) or path == metadata
                ):
                    yield path

    def _copy_is_valid(
        self,
        temporary: Path,
        expected_size: int,
        expected_hash: str,
        attempt: int,
    ) -> bool:
        del attempt
        return temporary.stat().st_size == expected_size and _sha256(temporary) == expected_hash

    def _copy_verified(
        self,
        source: Path,
        destination: Path,
        *,
        expected_size: int | None = None,
        expected_hash: str | None = None,
    ) -> ManifestEntry:
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_size = (
            source.stat(follow_symlinks=False).st_size if expected_size is None else expected_size
        )
        expected_hash = _sha256(source) if expected_hash is None else expected_hash
        for attempt in range(2):
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            try:
                source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_descriptor, "rb", closefd=True) as reader, temporary.open(
                    "xb"
                ) as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                if self._copy_is_valid(temporary, expected_size, expected_hash, attempt):
                    os.replace(temporary, destination)
                    _fsync_directory(destination.parent)
                    return ManifestEntry("", expected_size, expected_hash)
            finally:
                temporary.unlink(missing_ok=True)
            if attempt == 1:
                break
        raise UsbAgentError(f"copy verification failed: {source.name}")

    @staticmethod
    def _walk_regular_files(selected: Path) -> Iterator[Path]:
        stack = [selected]
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as children:
                entries = sorted(children, key=lambda entry: entry.name.casefold(), reverse=True)
            for entry in entries:
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path

    @staticmethod
    def _scan_batches(paths: Sequence[Path]) -> Iterator[tuple[Path, ...]]:
        batch: list[Path] = []
        batch_bytes = 0
        argv_bytes = 0
        for path in paths:
            size = path.stat(follow_symlinks=False).st_size
            path_bytes = len(os.fsencode(str(path))) + 1
            if batch and (
                len(batch) >= 256
                or batch_bytes + size > 8 * 1024 * 1024
                or argv_bytes + path_bytes > 32 * 1024
            ):
                yield tuple(batch)
                batch = []
                batch_bytes = 0
                argv_bytes = 0
            batch.append(path)
            batch_bytes += size
            argv_bytes += path_bytes
        if batch:
            yield tuple(batch)

    def _scan_batch(self, paths: Sequence[Path], root: Path) -> None:
        root = root.resolve(strict=True)
        try:
            result = self.run_command(
                (
                    "clamdscan",
                    "--stream",
                    "--infected",
                    "--no-summary",
                    "--",
                    *(str(path) for path in paths),
                )
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
            raise ScannerUnavailable("ClamAV scanner is unavailable") from error
        if result.returncode == 0:
            return
        if result.returncode != 1:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise ScannerUnavailable(f"ClamAV scan failed: {detail}")

        # Never parse scanner output: filenames are arbitrary POSIX data.  A
        # known-path single-file probe safely localizes an infected batch.
        threats: list[str] = []
        for path in paths:
            try:
                probe = self.run_command(
                    (
                        "clamdscan",
                        "--stream",
                        "--infected",
                        "--no-summary",
                        "--",
                        str(path),
                    )
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
                raise ScannerUnavailable("ClamAV scanner is unavailable") from error
            if probe.returncode == 1:
                threats.append(path.relative_to(root).as_posix())
            elif probe.returncode != 0:
                detail = probe.stderr.strip() or probe.stdout.strip() or "no diagnostic output"
                raise ScannerUnavailable(f"ClamAV scan failed: {detail}")
        if threats:
            raise SecurityBlocked(threats)
        raise ScannerUnavailable("ClamAV reported an infected batch without a localizable file")

    def scan_and_import(
        self,
        device_id: str,
        selected_relative_path: str,
        staging_directory: Path,
        metadata_relative_path: str | None = None,
        progress: ScanImportProgressCallback | None = None,
    ) -> ImportManifest:
        if not self._scan_import_lock.acquire(blocking=False):
            raise UsbAgentError("USB source is busy")
        try:
            return self._scan_and_import_locked(
                device_id,
                selected_relative_path,
                staging_directory,
                metadata_relative_path,
                progress,
            )
        finally:
            self._scan_import_lock.release()

    def _scan_and_import_locked(
        self,
        device_id: str,
        selected_relative_path: str,
        staging_directory: Path,
        metadata_relative_path: str | None = None,
        progress: ScanImportProgressCallback | None = None,
    ) -> ImportManifest:
        """Atomically scan and import one source under one device lease."""
        device = self._device(device_id)
        root = self._mounted_root(device, writable=False).resolve(strict=True)
        lease = self._ownership_leases.get(device.device_id)
        if lease is not None:
            local = self._local_path(staging_directory, must_exist=False)
            self._ownership.bind_job(device, local.parent.name, lease)
        selected = self._contained_path(root, selected_relative_path, directory=True)
        metadata: Path | None = None
        if metadata_relative_path:
            metadata = self._contained_path(root, metadata_relative_path, directory=False)
            if metadata.suffix.lower() != ".csv":
                raise UsbAgentError("metadata file must be CSV")
            try:
                metadata.relative_to(selected)
            except ValueError as error:
                raise UsbAgentError("metadata file must be inside selected tree") from error
        self._validate_signatures()

        # Start liveness before the potentially slow snapshot hash phase.
        state = {"stage": "scanning", "count": 0, "total": 0, "bytes": 0, "total_bytes": 0}
        stop = threading.Event()

        def emit(
            stage: str,
            count: int,
            total: int,
            done_bytes: int,
            total_bytes: int,
            heartbeat: bool = False,
        ) -> None:
            state.update(stage=stage, count=count, total=total, bytes=done_bytes, total_bytes=total_bytes)
            if progress is not None:
                progress(stage, count, total, done_bytes, total_bytes, heartbeat)

        def heartbeat() -> None:
            while not stop.wait(4.0):
                if progress is not None:
                    progress(
                        cast(str, state["stage"]), cast(int, state["count"]),
                        cast(int, state["total"]), cast(int, state["bytes"]),
                        cast(int, state["total_bytes"]), True,
                    )

        monitor = threading.Thread(target=heartbeat, name="usb-progress-heartbeat", daemon=True)
        monitor.start()
        try:
            scanner_files = tuple(self._walk_regular_files(selected))
            imports = tuple(self._walk_import_files(selected, metadata))
            snapshot: dict[Path, tuple[int, str, int, int]] = {}
            for path in scanner_files:
                stat = path.stat(follow_symlinks=False)
                snapshot[path] = (stat.st_size, _sha256(path), stat.st_ino, stat.st_mtime_ns)
            scan_bytes = sum(size for size, *_rest in snapshot.values())
            import_images = sum(_is_image_candidate(path) for path in imports)
            import_bytes = sum(path.stat(follow_symlinks=False).st_size for path in imports)
            destination = self._local_path(staging_directory, must_exist=False)
            if destination.exists():
                raise UsbAgentError("staging directory already exists")
            required_bytes = int(import_bytes * 2.2) + self.reserve_bytes
            if shutil.disk_usage(destination.parent).free < required_bytes:
                raise UsbAgentError("insufficient local storage for import")
        except BaseException:
            stop.set()
            monitor.join(timeout=1)
            raise

        emit("scanning", 0, len(scanner_files), 0, scan_bytes)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        entries: list[ManifestEntry] = []
        try:
            scanned = 0
            scanned_bytes = 0
            for batch in self._scan_batches(scanner_files):
                self._scan_batch(batch, root)
                scanned += len(batch)
                scanned_bytes += sum(snapshot[path][0] for path in batch)
                emit("scanning", scanned, len(scanner_files), scanned_bytes, scan_bytes)

            # Revalidate every scanner-accepted object before any staging write.
            current_device = self._device(device_id)
            current_root = self._mounted_root(current_device, writable=False)
            if current_device != device or current_root.resolve() != root.resolve():
                raise DeviceUnavailable("USB device or mount changed during scan")
            if set(self._walk_regular_files(selected)) != set(scanner_files):
                raise UsbAgentError("source tree changed after malware scan")
            for path in scanner_files:
                size, digest, inode, mtime_ns = snapshot[path]
                current = path.stat(follow_symlinks=False)
                if (
                    current.st_size != size
                    or current.st_ino != inode
                    or current.st_mtime_ns != mtime_ns
                    or _sha256(path) != digest
                ):
                    raise UsbAgentError("source changed after malware scan")

            temporary.mkdir()
            processed_images = 0
            processed_bytes = 0
            if import_images == 0:
                emit("importing", 0, 0, 0, import_bytes)
            for source in imports:
                relative = source.relative_to(selected).as_posix()
                expected_size, expected_hash, *_rest = snapshot[source]
                copied = self._copy_verified(
                    source,
                    temporary / relative,
                    expected_size=expected_size,
                    expected_hash=expected_hash,
                )
                entries.append(ManifestEntry(relative, copied.size_bytes, copied.sha256))
                processed_bytes += expected_size
                if _is_image_candidate(source):
                    processed_images += 1
                if processed_images % self.copy_progress_images == 0 or processed_images == import_images:
                    emit("importing", processed_images, import_images, processed_bytes, import_bytes)

            manifest = ImportManifest(
                schema_version="1.0",
                device_id=device.device_id,
                device_uuid=device.uuid,
                device_serial=device.serial,
                selected_relative_path=PurePosixPath(selected_relative_path).as_posix(),
                created_at=datetime.now(UTC).isoformat(),
                entries=tuple(sorted(entries, key=lambda entry: entry.relative_path)),
            )
            manifest_path = temporary / "import_manifest.json"
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(asdict(manifest), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(temporary)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
            return manifest
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        finally:
            stop.set()
            monitor.join(timeout=1)

    def _local_path(self, path: Path, *, must_exist: bool, directory: bool | None = None) -> Path:
        root = self.job_root.resolve(strict=True)
        try:
            relative = path.absolute().relative_to(root)
        except ValueError as error:
            raise UsbAgentError("local path is outside configured job root") from error
        return self._contained_path(
            root,
            relative.as_posix(),
            must_exist=must_exist,
            directory=directory,
        )

    def copy_to_staging(
        self,
        device_id: str,
        selected_relative_path: str,
        staging_directory: Path,
        metadata_relative_path: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> ImportManifest:
        device = self._device(device_id)
        root = self._mounted_root(device, writable=False)
        selected = self._contained_path(root, selected_relative_path, directory=True)
        metadata: Path | None = None
        if metadata_relative_path:
            metadata = self._contained_path(root, metadata_relative_path, directory=False)
            if metadata.suffix.lower() != ".csv":
                raise UsbAgentError("metadata file must be CSV")
            try:
                metadata.relative_to(selected)
            except ValueError as error:
                raise UsbAgentError("metadata file must be inside selected tree") from error

        self.scan_selected_tree(device_id, selected_relative_path)
        sources = tuple(self._walk_import_files(selected, metadata))
        total_images = sum(_is_image_candidate(path) for path in sources)
        source_bytes = sum(path.stat(follow_symlinks=False).st_size for path in sources)
        destination = self._local_path(staging_directory, must_exist=False)
        if destination.exists():
            raise UsbAgentError("staging directory already exists")
        required_bytes = int(source_bytes * 2.2) + self.reserve_bytes
        if shutil.disk_usage(destination.parent).free < required_bytes:
            raise UsbAgentError("insufficient local storage for import")

        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        temporary.mkdir()
        entries: list[ManifestEntry] = []
        processed_images = 0
        try:
            for source in sources:
                relative = source.relative_to(selected).as_posix()
                copied = self._copy_verified(source, temporary / relative)
                entries.append(ManifestEntry(relative, copied.size_bytes, copied.sha256))
                if _is_image_candidate(source):
                    processed_images += 1
                    if progress is not None and (
                        processed_images % self.copy_progress_images == 0
                        or processed_images == total_images
                    ):
                        progress(processed_images, total_images)
            manifest = ImportManifest(
                schema_version="1.0",
                device_id=device.device_id,
                device_uuid=device.uuid,
                device_serial=device.serial,
                selected_relative_path=PurePosixPath(selected_relative_path).as_posix(),
                created_at=datetime.now(UTC).isoformat(),
                entries=tuple(sorted(entries, key=lambda entry: entry.relative_path)),
            )
            manifest_path = temporary / "import_manifest.json"
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(asdict(manifest), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(temporary)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
            return manifest
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def preflight_selected_tree(
        self,
        device_id: str,
        selected_relative_path: str,
        metadata_relative_path: str | None = None,
    ) -> PreflightSummary:
        progress = getattr(self._progress_local, "preflight", None)
        device = self._device(device_id)
        root = self._mounted_root(device, writable=False)
        selected = self._contained_path(root, selected_relative_path, directory=True)
        metadata: Path | None = None
        if metadata_relative_path:
            metadata = self._contained_path(root, metadata_relative_path, directory=False)
            try:
                metadata.relative_to(selected)
            except ValueError as error:
                raise UsbAgentError("metadata file must be inside selected tree") from error
            if metadata.suffix.lower() != ".csv":
                raise UsbAgentError("metadata file must be CSV")
        file_count = 0
        for _ in self._walk_import_files(selected, metadata):
            file_count += 1
            if progress is not None and (file_count % 500 == 0):
                progress("discovering", file_count, None)
        if progress is not None:
            progress("discovering", file_count, file_count)

        source_bytes = 0
        measured = 0
        fingerprint = hashlib.sha256()
        for path in self._walk_import_files(selected, metadata):
            stat = path.stat(follow_symlinks=False)
            source_bytes += stat.st_size
            fingerprint.update(
                f"{path.relative_to(selected).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_ino}\n".encode()
            )
            measured += 1
            if progress is not None and (measured % 500 == 0 or measured == file_count):
                progress("measuring", measured, file_count)
        if measured != file_count:
            raise UsbAgentError("source changed during preflight")
        self._preflight_fingerprints[
            (device_id, selected_relative_path, metadata_relative_path)
        ] = fingerprint.hexdigest()
        required = int(source_bytes * 2.2) + self.reserve_bytes
        free = shutil.disk_usage(self.job_root.resolve(strict=True)).free
        return PreflightSummary(file_count, source_bytes, required, free, free >= required)

    def get_preflight_fingerprint(
        self,
        device_id: str,
        selected_relative_path: str,
        metadata_relative_path: str | None = None,
    ) -> str:
        try:
            return self._preflight_fingerprints[
                (device_id, selected_relative_path, metadata_relative_path)
            ]
        except KeyError as error:
            raise UsbAgentError("preflight source snapshot is unavailable") from error

    def fingerprint_selected_tree(
        self,
        device_id: str,
        selected_relative_path: str,
        metadata_relative_path: str | None = None,
    ) -> str:
        device = self._device(device_id)
        root = self._mounted_root(device, writable=False)
        selected = self._contained_path(root, selected_relative_path, directory=True)
        metadata: Path | None = None
        if metadata_relative_path:
            metadata = self._contained_path(root, metadata_relative_path, directory=False)
            try:
                metadata.relative_to(selected)
            except ValueError as error:
                raise UsbAgentError("metadata file must be inside selected tree") from error
            if metadata.suffix.lower() != ".csv":
                raise UsbAgentError("metadata file must be CSV")
        fingerprint = hashlib.sha256()
        for path in self._walk_import_files(selected, metadata):
            stat = path.stat(follow_symlinks=False)
            fingerprint.update(
                f"{path.relative_to(selected).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_ino}\n".encode()
            )
        return fingerprint.hexdigest()

    def export_results(
        self,
        device_id: str,
        selected_relative_path: str,
        output_directory: Path,
    ) -> str:
        device = self._device(device_id)
        outputs = self._local_path(output_directory, must_exist=True, directory=True)
        names = {entry.name for entry in outputs.iterdir()}
        # Resume already-generated legacy bundles without rewriting historical results.
        if names == set(OUTPUT_FILES):
            output_files = OUTPUT_FILES
        elif names == set(LEGACY_OUTPUT_FILES):
            output_files = LEGACY_OUTPUT_FILES
        else:
            raise UsbAgentError("invalid result bundle file set")
        source_files = [
            self._contained_path(outputs, name, directory=False) for name in output_files
        ]
        root = self._export_root(device, outputs, selected_relative_path)
        selected = self._contained_path(root, selected_relative_path, directory=True)

        for candidate in sorted(selected.iterdir(), key=lambda path: path.name):
            if candidate.is_symlink() or not RESULT_DIRECTORY.fullmatch(candidate.name):
                continue
            try:
                entries = tuple(candidate.iterdir())
                if {entry.name for entry in entries} != set(output_files) or any(
                    entry.is_symlink() or not entry.is_file() for entry in entries
                ):
                    continue
                if all(
                    source.stat().st_size == (candidate / source.name).stat().st_size
                    and _sha256(source) == _sha256(candidate / source.name)
                    for source in source_files
                ):
                    return candidate.relative_to(root).as_posix()
            except OSError:
                continue

        used = [
            int(match.group(1))
            for child in selected.iterdir()
            if not child.is_symlink() and (match := RESULT_DIRECTORY.fullmatch(child.name))
        ]
        sequence = max(used, default=0) + 1
        if sequence > 999:
            raise UsbAgentError("result directory sequence exhausted")
        name = f"inference_results_{sequence:03d}"
        destination = selected / name
        temporary = selected / f".{name}.{uuid4().hex}.tmp"
        temporary.mkdir()
        try:
            for source in source_files:
                self._copy_verified(source, temporary / source.name)
            _fsync_directory(temporary)
            os.replace(temporary, destination)
            _fsync_directory(selected)
            return destination.relative_to(root.resolve(strict=True)).as_posix()
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _export_root(self, device: Device, outputs: Path, selected: str) -> Path:
        lease = self._ownership_leases.get(device.device_id)
        if lease is not None:
            self._ownership._check_lease(device, lease)
            mounts = self._mount_infos(device, service_target=lease.owned_root)
            if any(target == lease.owned_root for target, _ in mounts) and self._verify_owned_view(device, lease):
                return lease.owned_root

        # Recovery is not an ordinary export remount. A path is only a lookup
        # key: the durable job and broker-held logical owner must authorize it.
        job_id = outputs.parent.name
        try:
            if outputs.name != "outputs" or str(UUID(job_id)) != job_id:
                raise ValueError
            database = self.job_root.parent / "app.db"
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
                row = connection.execute(
                    "SELECT j.state, r.device_uuid, r.device_serial, r.source_relative_path "
                    "FROM jobs j JOIN job_requests r USING(job_id) WHERE j.job_id = ?",
                    (job_id,),
                ).fetchone()
            if (row is None or row[0] not in {"EXPORTING", "WAITING_FOR_USB"}
                or row[1] != device.uuid or (row[2] and row[2] != device.serial)
                or row[3] != selected):
                raise ValueError
        except (ValueError, sqlite3.Error) as error:
            raise UsbAgentError("original job recovery is not authorized") from error
        try:
            recovered = self._ownership.recover(device, job_id, lease)
            self._remember_ownership(device, recovered)
            return recovered.owned_root
        except UsbAgentError as error:
            if str(error) in {"USB ownership recovery is pending", "USB mount broker is unavailable"}:
                raise DeviceUnavailable("USB ownership recovery is pending") from error
            raise
        except OwnershipUnavailable as error:
            raise DeviceUnavailable(str(error)) from error
        except OwnershipError as error:
            raise UsbAgentError(str(error)) from error

    def flush(self, device_id: str) -> None:
        device = self._device(device_id)
        _fsync_directory(self._mounted_root(device, writable=True))

    def release(self, device_id: str) -> None:
        """Release the exclusive lease, then restore one native desktop RW mount."""
        device = self._device(device_id)
        lease = self._ownership_leases.get(device.device_id)
        if lease is None:
            return
        try:
            released = self._ownership.release(device, lease)
        except OwnershipError as error:
            raise UsbAgentError(str(error)) from error
        if not released:
            raise DeviceUnavailable("USB ownership lease is unavailable")
        if not self._live_broker and lease.native_user is not None:
            self._mount_as_user(device, lease.native_user, writable=True)
        self._ownership_leases.pop(device.device_id, None)

    def eject(self, device_id: str) -> None:
        device = self._device(device_id)
        lease = self._ownership_leases.get(device.device_id)
        if lease is None:
            raise DeviceUnavailable("USB ownership lease is unavailable")
        try:
            ejected = self._ownership.eject(device, lease)
        except OwnershipError as error:
            raise UsbAgentError(str(error)) from error
        if not ejected:
            raise DeviceUnavailable("USB ownership lease is unavailable")
        self._ownership_leases.pop(device.device_id, None)
        if not self._live_broker:
            self._command(
                (
                    "udisksctl",
                    "power-off",
                    "--block-device",
                    device.drive_device,
                    "--no-user-interaction",
                )
            )
