"""Narrow root broker for one exclusive, service-owned USB mount."""

from __future__ import annotations

import array
import grp
import hashlib
import json
import logging
import os
import pwd
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOCKET_PATH = Path("/run/plankton-inference/usb-mount-broker.sock")
LEASES_PATH = Path("/run/plankton-inference/usb-ownership-leases.json")
MOUNT_GUARD_DIR = Path("/run/plankton-usb-mount-guard")
SERVICE_USER = "plankton-usb"
SERVICE_ROOT = Path("/media/plankton-usb")
BLOCK_DEVICE = re.compile(r"^/dev/[A-Za-z0-9._-]+$")
MAX_REQUEST_BYTES = 16 * 1024
log = logging.getLogger("plankton-usb-mount-broker")
_TRANSITION_LOCK = threading.RLock()
JOB_DATABASES = (
    Path("/var/lib/plankton-inference/app.db"),
    Path("/var/lib/plankton-inference-test-current/app.db"),
)


class RecoveryPending(RuntimeError):
    pass


class BrokerError(RuntimeError):
    pass


def _run(argv: tuple[str, ...], *, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, check=False, text=True, timeout=30, pass_fds=pass_fds
    )


def _run_in_namespace(namespace_fd: int, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    if namespace_fd < 0:
        raise BrokerError("invalid consumer namespace")
    return _run(
        ("/usr/bin/nsenter", f"--mount=/proc/self/fd/{namespace_fd}", "--", *argv),
        pass_fds=(namespace_fd,),
    )


def _command(
    argv: tuple[str, ...], *, namespace_fd: int | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = _run(argv) if namespace_fd is None else _run_in_namespace(namespace_fd, argv)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BrokerError(f"command unavailable: {argv[0]}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise BrokerError(f"{argv[0]} failed: {detail}")
    return result


def _validate_request(request: object) -> dict[str, str]:
    if not isinstance(request, dict):
        raise BrokerError("request must be an object")
    values: dict[str, str] = {}
    for key in (
        "operation",
        "block_device",
        "drive_device",
        "uuid",
        "serial",
        "owned_root",
        "consumer_id",
        "lease_id",
        "generation",
        "native_user",
        "job_id",
    ):
        value = request.get(key, "")
        if not isinstance(value, str) or len(value) > 4096:
            raise BrokerError(f"invalid {key}")
        values[key] = value
    if values["operation"] not in {
        "claim",
        "release",
        "reconcile",
        "eject",
        "recover",
        "job_lease",
        "bind_job",
    }:
        raise BrokerError("unsupported mount broker operation")
    if not BLOCK_DEVICE.fullmatch(values["block_device"]):
        raise BrokerError("invalid block device")
    if values["drive_device"] and not BLOCK_DEVICE.fullmatch(values["drive_device"]):
        raise BrokerError("invalid drive device")
    if not values["uuid"]:
        raise BrokerError("missing filesystem UUID")
    if values["consumer_id"] != SERVICE_USER:
        raise BrokerError("unauthorized ownership consumer")
    if values["native_user"]:
        raise BrokerError("native restoration authority is lease-bound")
    if values["operation"] == "claim" and (
        values["owned_root"] or values["lease_id"] or values["generation"]
    ):
        raise BrokerError("claim inputs must be broker-derived")
    if values["operation"] in {"release", "eject", "recover", "bind_job"} and (
        not values["lease_id"] or not values["generation"]
    ):
        raise BrokerError("opaque ownership lease is required")
    if values["operation"] == "reconcile" and (
        values["owned_root"] or values["lease_id"] or values["generation"]
    ):
        raise BrokerError("reconcile inputs must be broker-derived")
    if values["operation"] in {"recover", "job_lease", "bind_job"}:
        try:
            if str(uuid.UUID(values["job_id"])) != values["job_id"]:
                raise ValueError
        except ValueError as error:
            raise BrokerError("invalid recovery job") from error
    return values


def _safe_path(path: str) -> Path:
    value = Path(path)
    if not value.is_absolute() or value != value.resolve(strict=False):
        raise BrokerError("mount path must not contain symlinks")
    return value


def _validate_owned_path(path: str) -> Path:
    owned = _safe_path(path)
    if owned.parent != SERVICE_ROOT or not owned.name:
        raise BrokerError("invalid owned mount path")
    return owned


def _identity(request: dict[str, str]) -> None:
    fields = _command(
        ("/usr/bin/lsblk", "--noheadings", "--raw", "--output", "UUID,SERIAL", request["block_device"])
    ).stdout.split()
    if not fields or fields[0] != request["uuid"]:
        raise BrokerError("filesystem identity changed")
    if request["serial"] and (len(fields) < 2 or fields[1] != request["serial"]):
        fields = _command(
            (
                "/usr/bin/lsblk",
                "--noheadings",
                "--raw",
                "--output",
                "SERIAL",
                request["drive_device"],
            )
        ).stdout.split()
        if not fields or fields[0] != request["serial"]:
            raise BrokerError("hardware identity changed")


def _lease_key(request: dict[str, str]) -> str:
    return "\0".join((request["block_device"], request["uuid"], request["serial"]))


def _guard_marker(block: str) -> Path:
    if not BLOCK_DEVICE.fullmatch(block):
        raise BrokerError("invalid block device")
    digest = hashlib.sha256(block.encode("ascii")).hexdigest()
    return MOUNT_GUARD_DIR / digest


def _guard_acquire(block: str) -> None:
    MOUNT_GUARD_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    marker = _guard_marker(block)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o644,
    )
    try:
        os.fchmod(descriptor, 0o644)
        os.write(descriptor, block.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _guard_release(block: str) -> None:
    if not MOUNT_GUARD_DIR.is_dir():
        return
    try:
        _guard_marker(block).unlink()
    except FileNotFoundError:
        pass


def _lease_guard_blocks(lease: dict[str, str]) -> set[str]:
    try:
        recorded = json.loads(lease.get("guard_blocks", "[]"))
    except (ValueError, TypeError) as error:
        raise BrokerError("invalid guard responsibility") from error
    if not isinstance(recorded, list) or any(
        not isinstance(block, str) or not BLOCK_DEVICE.fullmatch(block) for block in recorded
    ):
        raise BrokerError("invalid guard responsibility")
    return set(recorded) | {
        lease[field] for field in ("block_device", "recovery_block") if lease.get(field)
    }


def _retire_lease_guards(
    lease: dict[str, str], remaining: dict[str, dict[str, str]], *, current_block: str = ""
) -> None:
    retained = {block for owner in remaining.values() for block in _lease_guard_blocks(owner)}
    touched = _lease_guard_blocks(lease) | ({current_block} if current_block else set())
    try:
        for block in sorted(touched - retained):
            _guard_release(block)
    except OSError as error:
        # The caller has not finalized its durable lease yet: retry/restart
        # must still know every marker which belongs to this transition.
        raise RecoveryPending from error


def _reconcile_guard_markers() -> None:
    leases = _load_leases()
    guarded_blocks = {block for lease in leases.values() for block in _lease_guard_blocks(lease)}
    active_markers = {_guard_marker(block).name for block in guarded_blocks}
    MOUNT_GUARD_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    for marker in MOUNT_GUARD_DIR.iterdir():
        if marker.name not in active_markers:
            marker.unlink()
    for block in guarded_blocks:
        marker = _guard_marker(block)
        if not marker.exists():
            _guard_acquire(block)


def _load_leases() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(LEASES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as error:
        raise BrokerError("ownership ledger is unavailable") from error
    if not isinstance(raw, dict) or any(
        not isinstance(key, str) or not isinstance(record, dict)
        or any(not isinstance(field, str) or not isinstance(value, str)
               for field, value in record.items())
        for key, record in raw.items()
    ):
        raise BrokerError("invalid ownership ledger")
    return raw


def _save_leases(leases: dict[str, dict[str, str]]) -> None:
    LEASES_PATH.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{LEASES_PATH.name}.", dir=LEASES_PATH.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o660)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(leases, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, LEASES_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _lease_for(request: dict[str, str]) -> dict[str, str] | None:
    leases = _load_leases()
    direct = leases.get(_lease_key(request))
    if direct is not None and direct.get("uuid") == request["uuid"] and direct.get(
        "serial", ""
    ) == request["serial"]:
        return direct
    matches = [
        lease
        for lease in leases.values()
        if lease.get("uuid") == request["uuid"]
        and lease.get("serial", "") == request["serial"]
    ]
    if len(matches) > 1:
        raise BrokerError("ambiguous ownership lease")
    return matches[0] if matches else None


def _remove_lease(
    leases: dict[str, dict[str, str]], request: dict[str, str], lease: dict[str, str]
) -> None:
    lease_id = lease.get("lease_id", "")
    if lease_id:
        for key, candidate in tuple(leases.items()):
            if candidate.get("lease_id") == lease_id and (
                not lease.get("generation")
                or candidate.get("generation") == lease.get("generation")
            ):
                leases.pop(key, None)
    # Keep direct callers and legacy records compatible when an opaque lease
    # lacks its historical identity fields.
    leases.pop(_lease_key(request), None)


def _mounts(block: str, namespace_fd: int | None = None) -> dict[Path, frozenset[str]]:
    argv = ("/usr/bin/findmnt", "--json", "--list", "--output", "SOURCE,TARGET,OPTIONS")
    try:
        result = _run(argv) if namespace_fd is None else _run_in_namespace(namespace_fd, argv)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BrokerError("findmnt is unavailable") from error
    if result.returncode == 1:
        return {}
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise BrokerError(f"findmnt failed: {detail}")
    try:
        filesystems = json.loads(result.stdout)["filesystems"]
        mounts: dict[Path, frozenset[str]] = {}
        for item in filesystems:
            source = str(item.get("source") or "")
            if source != block and not source.startswith(block + "["):
                continue
            target = Path(str(item["target"]))
            if target.is_absolute():
                mounts[target] = frozenset(str(item["options"]).split(","))
        return mounts
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise BrokerError("invalid mount table") from error


def _mounted(mounts: dict[Path, frozenset[str]], path: Path, mode: str) -> bool:
    options = mounts.get(path)
    return options is not None and mode in options


def _native_user(mounts: dict[Path, frozenset[str]]) -> str | None:
    for target in mounts:
        parts = target.parts
        user = (
            parts[2]
            if len(parts) == 4 and parts[:2] == ("/", "media")
            else parts[3]
            if len(parts) == 5 and parts[:3] == ("/", "run", "media")
            else None
        )
        if user and user != SERVICE_USER and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", user):
            return user
    return None


def _native_mount_for_user(mounts: dict[Path, frozenset[str]], user: str) -> bool:
    for target, options in mounts.items():
        if (
            _native_user({target: options}) == user
            and _mounted(mounts, target, "rw")
            and {"nodev", "nosuid"}.issubset(options)
        ):
            return True
    return False


def _active_local_session(user: str) -> bool:
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return False
    bus = Path(f"/run/user/{uid}/bus")
    if not bus.is_socket():
        return False
    try:
        sessions = _run(
            ("/usr/bin/loginctl", "show-user", user, "--property=Sessions", "--value")
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if sessions.returncode != 0:
        return False
    for session in sessions.stdout.split():
        try:
            details = _run(
                (
                    "/usr/bin/loginctl",
                    "show-session",
                    session,
                    "--property=User",
                    "--property=Active",
                    "--property=Remote",
                    "--property=State",
                    "--property=Type",
                )
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        values = {
            key: value
            for line in details.stdout.splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
        if (
            details.returncode == 0
            and values.get("User") == str(uid)
            and values.get("Active") == "yes"
            and values.get("Remote") == "no"
            and values.get("State") == "active"
            and values.get("Type") in {"x11", "wayland"}
        ):
            return True
    return False


def _run_as_native_user(user: str, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return _run(("/usr/sbin/runuser", "--user", user, "--", *argv))


def _native_mount_target(block: str, user: str) -> Path | None:
    try:
        mounts = _mounts(block)
    except BrokerError:
        return None
    targets = tuple(
        target for target, options in mounts.items() if _native_user({target: options}) == user
    )
    return targets[0] if len(targets) == 1 else None


def _nautilus_holds(block: str, user: str) -> bool | None:
    target = _native_mount_target(block, user)
    if target is None:
        return None
    try:
        result = _run_as_native_user(user, ("/usr/bin/fuser", "--mount", str(target)))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        return None
    pids = tuple(re.findall(r"\b[1-9][0-9]*\b", result.stdout + result.stderr))
    if not pids:
        return None
    for pid in pids:
        try:
            process = _run_as_native_user(user, ("/usr/bin/ps", "-o", "comm=", "-p", pid))
        except (OSError, subprocess.TimeoutExpired):
            return None
        if process.returncode != 0:
            return None
        if process.stdout.strip() == "nautilus":
            return True
    return False


def _wait_for_nautilus_release(block: str, user: str) -> bool:
    deadline = time.monotonic() + 5.0
    while True:
        state = _nautilus_holds(block, user)
        if state is False:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _close_nautilus(_block: str, user: str) -> None:
    if not _active_local_session(user):
        raise BrokerError("native mount is busy")
    try:
        _run(
            (
                "/usr/bin/systemd-run",
                "--user",
                f"--machine={user}@.host",
                "--wait",
                "--pipe",
                "--quiet",
                "/usr/bin/nautilus",
                "--quit",
            )
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BrokerError("native mount is busy") from error
    # `nautilus --quit` may return nonzero after closing its last window. Its
    # holder state is the authoritative bounded postcondition before retry.
    if not _wait_for_nautilus_release(_block, user):
        raise BrokerError("native mount is busy")


def _unmount_device(block: str) -> None:
    try:
        _command(("/usr/bin/udisksctl", "unmount", "--block-device", block, "--no-user-interaction"))
    except BrokerError as error:
        detail = str(error).lower()
        if "busy" in detail or "already mounted" in detail:
            native_user = _native_user(_mounts(block))
            if native_user is not None:
                _close_nautilus(block, native_user)
                unmount_argv = (
                    "/usr/bin/udisksctl",
                    "unmount",
                    "--block-device",
                    block,
                    "--no-user-interaction",
                )
                deadline = time.monotonic() + 5.0
                while True:
                    try:
                        _command(unmount_argv)
                        return
                    except BrokerError as retry_error:
                        detail = str(retry_error).lower()
                        if "busy" not in detail and "already mounted" not in detail:
                            raise
                        if time.monotonic() >= deadline:
                            raise BrokerError("native mount is busy") from retry_error
                        time.sleep(0.1)
            raise BrokerError("native mount is busy") from error
        raise


def _udisks_block_object_path(block_device: str) -> str:
    name = Path(block_device).name
    encoded = "".join(
        char if char.isascii() and char.isalnum() else "_5f" if char == "_" else f"_{ord(char):02x}"
        for char in name
    )
    return f"/org/freedesktop/UDisks2/block_devices/{encoded}"


def _restore_native(block: str, user: str) -> None:
    try:
        pwd.getpwnam(user)
    except KeyError as error:
        raise BrokerError("native USB view was not restored") from error
    if _native_mount_for_user(_mounts(block), user):
        return
    try:
        _command(
            (
                "/usr/bin/gdbus",
                "call",
                "--system",
                "--dest",
                "org.freedesktop.UDisks2",
                "--object-path",
                _udisks_block_object_path(block),
                "--method",
                "org.freedesktop.UDisks2.Filesystem.Mount",
                f"{{'options': <'rw,nodev,nosuid,noexec'>, 'as-user': <'{user}'>}}",
            )
        )
    except BrokerError:
        if _native_mount_for_user(_mounts(block), user):
            return
        raise BrokerError("native USB view was not restored")
    if not _native_mount_for_user(_mounts(block), user):
        raise BrokerError("native USB view was not restored")


def _mount_owned(block: str, target: Path, *, namespace_fd: int | None = None) -> None:
    _command(
        (
            "/usr/bin/mount",
            "--options",
            "rw,nodev,nosuid,noexec",
            block,
            str(target),
        ),
        namespace_fd=namespace_fd,
    )


def _unmount(path: Path, *, lazy: bool = False, namespace_fd: int | None = None) -> None:
    _command(
        ("/usr/bin/umount", *(("--lazy",) if lazy else ()), str(path)),
        namespace_fd=namespace_fd,
    )


def _remove_owned_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _begin_claim(request: dict[str, str], owned: Path) -> dict[str, str]:
    leases = _load_leases()
    key = _lease_key(request)
    previous = leases.get(key)
    generation = int(previous.get("generation", "0")) + 1 if previous else 1
    lease = {
        "lease_id": uuid.uuid4().hex,
        "generation": str(generation),
        "consumer_id": SERVICE_USER,
        "block_device": request["block_device"],
        "uuid": request["uuid"],
        "serial": request["serial"],
        "drive_device": request["drive_device"],
        "owned_root": str(owned),
        "native_user": request["native_user"],
        "diskseq": _diskseq(request["drive_device"]),
        "cleanup_pending": "1",
        "cleanup_target": "native_rw" if request["native_user"] else "unmounted",
        "transition_stage": "prepared",
    }
    leases[key] = lease
    # Persist responsibility before any command can remove the native view.
    _save_leases(leases)
    return lease


def _save_claim(lease: dict[str, str], **updates: str) -> None:
    lease.update(updates)
    leases = _load_leases()
    leases[_lease_key(lease)] = lease
    _save_leases(leases)


def _record_claim_error(lease: dict[str, str], field: str, error: BaseException) -> None:
    # Full exception chains stay in the host journal; this private ledger survives restart.
    try:
        _save_claim(lease, **{field: f"{type(error).__name__}: {error}"[:2048]})
    except Exception:
        log.exception("could not persist %s; earlier cleanup responsibility retained", field)


def _issue_lease(request: dict[str, str], owned: Path) -> dict[str, Any]:
    leases = _load_leases()
    lease = leases[_lease_key(request)]
    if lease.get("cleanup_pending") != "1" or lease.get("owned_root") != str(owned):
        raise BrokerError("claim responsibility is unavailable")
    for field in ("cleanup_pending", "cleanup_target", "transition_stage"):
        lease.pop(field, None)
    lease["issued_at"] = datetime.now(UTC).isoformat()
    _save_leases(leases)
    return _lease_response(lease)


def _claim(request: dict[str, str], owned: Path, *, namespace_fd: int | None = None) -> dict[str, Any]:
    if namespace_fd is None:
        raise BrokerError("consumer namespace is required")
    native_mounts = _mounts(request["block_device"])
    native_user = _native_user(native_mounts)
    if native_mounts and not native_user:
        raise BrokerError("native restoration owner is unavailable")
    request["native_user"] = native_user or ""
    lease = _begin_claim(request, owned)
    try:
        _guard_acquire(request["block_device"])
        if native_mounts:
            _save_claim(lease, transition_stage="detaching_native")
            _unmount_device(request["block_device"])
            if _mounts(request["block_device"]):
                raise BrokerError("native mount is still present")
        consumer_mounts = _mounts(request["block_device"], namespace_fd)
        if owned in consumer_mounts:
            if _mounted(consumer_mounts, owned, "rw"):
                return _issue_lease(request, owned)
            raise BrokerError("owned mount has unsafe state")
        if owned.exists() and (not owned.is_dir() or any(owned.iterdir())):
            raise BrokerError("owned mount path is not empty")
        owned.mkdir(mode=0o700, parents=False, exist_ok=True)
        os.chown(owned, pwd.getpwnam(SERVICE_USER).pw_uid, grp.getgrnam("plankton-inference").gr_gid)
        _save_claim(lease, transition_stage="mounting_owned")
        _mount_owned(request["block_device"], owned, namespace_fd=namespace_fd)
        consumer_mounts = _mounts(request["block_device"], namespace_fd)
        if not _mounted(consumer_mounts, owned, "rw") or _mounts(request["block_device"]):
            raise BrokerError("exclusive ownership postcondition failed")
        return _issue_lease(request, owned)
    except BaseException as primary_error:
        log.exception("claim failed lease=%s stage=%s", lease["lease_id"], lease["transition_stage"])
        _record_claim_error(lease, "primary_error", primary_error)
        try:
            _release(request, owned, lease=lease, namespace_fd=namespace_fd)
        except Exception as rollback_error:
            log.exception("claim rollback failed lease=%s; cleanup pending", lease["lease_id"])
            _record_claim_error(lease, "rollback_error", rollback_error)
            raise BrokerError("USB ownership cleanup is pending") from rollback_error
        raise


def _check_lease(request: dict[str, str], lease: dict[str, str]) -> Path:
    for field in ("lease_id", "generation"):
        if request[field] and request[field] != lease.get(field):
            raise BrokerError("stale ownership lease")
    if lease.get("uuid") != request["uuid"] or lease.get("serial", "") != request["serial"]:
        raise BrokerError("stale ownership lease")
    owned = _validate_owned_path(lease.get("owned_root", ""))
    if request["owned_root"] and _validate_owned_path(request["owned_root"]) != owned:
        raise BrokerError("stale ownership lease")
    return owned


def _release(
    request: dict[str, str],
    owned: Path,
    *,
    lease: dict[str, str] | None = None,
    namespace_fd: int | None = None,
) -> dict[str, Any]:
    mounts = _mounts(request["block_device"], namespace_fd)
    # A failed/slow claim may outlive the device which originally passed validation.
    _identity(request)
    if owned in mounts:
        _unmount(owned, namespace_fd=namespace_fd)
        if owned in _mounts(request["block_device"], namespace_fd):
            raise BrokerError("owned mount did not release")
    _identity(request)
    _remove_owned_directory(owned)
    native_user = lease.get("native_user", "") if lease is not None else ""
    if native_user:
        _restore_native(request["block_device"], native_user)
    if lease is not None and lease.get("cleanup_pending"):
        native = _mounts(request["block_device"])
        if (
            (native_user and (len(native) != 1 or not _native_mount_for_user(native, native_user)))
            or (not native_user and native)
            or owned in _mounts(request["block_device"], namespace_fd)
        ):
            raise BrokerError("native rollback postcondition failed")
    _identity(request)
    leases = _load_leases()
    _remove_lease(leases, request, lease or {})
    _retire_lease_guards(lease or {}, leases, current_block=request["block_device"])
    _save_leases(leases)
    return {"ok": True, "released": True}


def _eject(
    request: dict[str, str],
    owned: Path,
    *,
    lease: dict[str, str] | None = None,
    namespace_fd: int | None = None,
) -> dict[str, Any]:
    mounts = _mounts(request["block_device"], namespace_fd)
    if owned in mounts:
        _unmount(owned, lazy=True, namespace_fd=namespace_fd)
        if owned in _mounts(request["block_device"], namespace_fd):
            raise BrokerError("controlled mount did not release")
    if _mounts(request["block_device"]):
        raise BrokerError("device is still mounted")
    _command(
        (
            "/usr/bin/udisksctl",
            "power-off",
            "--block-device",
            request["drive_device"],
            "--no-user-interaction",
        )
    )
    if _mounts(request["block_device"]):
        raise BrokerError("device remained mounted")
    _remove_owned_directory(owned)
    leases = _load_leases()
    _remove_lease(
        leases,
        request,
        lease
        or {
            "lease_id": request.get("lease_id", ""),
            "generation": request.get("generation", ""),
        },
    )
    _retire_lease_guards(lease or {}, leases, current_block=request["block_device"])
    _save_leases(leases)
    return {"ok": True, "released": True}


def _diskseq(drive: str) -> str:
    """Kernel incarnation, not a device-node/name guess; absent evidence fails closed."""
    if not BLOCK_DEVICE.fullmatch(drive):
        return ""
    try:
        value = (Path("/sys/class/block") / Path(drive).name / "diskseq").read_text().strip()
    except OSError:
        return ""
    return value if value.isdecimal() else ""


def _job_database(namespace_fd: int) -> Path:
    # Test's unit binds its state over the fixed production path. Resolve by
    # inode, never by a path/selector supplied in the recovery request.
    observed = _command(
        ("/usr/bin/stat", "-Lc", "%d:%i", "/var/lib/plankton-inference/app.db"),
        namespace_fd=namespace_fd,
    ).stdout.strip()
    for candidate in JOB_DATABASES:
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if observed == f"{stat.st_dev}:{stat.st_ino}":
            return candidate.resolve(strict=True)
    raise BrokerError("recovery database is not an installed product database")


def _check_job(
    connection: sqlite3.Connection,
    request: dict[str, str],
    lease: dict[str, str],
    *,
    binding: bool = False,
) -> str:
    issued = lease.get("issued_at", "")
    if not issued:
        raise BrokerError("lease has no original job authority")
    # A lease can transfer only to the first durable job created during its
    # lifetime. A later job must never adopt a terminal job's retained lease.
    row = connection.execute(
        "SELECT j.job_id, j.state, r.device_uuid, r.device_serial FROM jobs j "
        "JOIN job_requests r USING(job_id) WHERE j.started_at >= ? "
        "ORDER BY j.started_at, j.job_id LIMIT 1",
        (issued,),
    ).fetchone()
    if (
        row is None
        or (request["job_id"] and row[0] != request["job_id"])
        or row[1] in {"COMPLETED", "FAILED", "SECURITY_BLOCKED", "CANCELLED"}
        or row[2] != request["uuid"]
        or (row[3] and row[3] != request["serial"])
        or (lease.get("job_id") and lease["job_id"] != row[0])
        or (not binding and not lease.get("job_id"))
        or (binding and row[1] not in {"SCANNING", "IMPORTING"})
    ):
        raise BrokerError("original job ownership is no longer authorized")
    return str(row[0])


@contextmanager
def _job_authority(
    request: dict[str, str], lease: dict[str, str], namespace_fd: int, *, binding: bool = False
) -> Iterator[sqlite3.Connection]:
    database = _job_database(namespace_fd)
    stat = database.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    if lease.get("database") and lease["database"] != identity:
        raise BrokerError("job database identity changed")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True, timeout=1)
    try:
        # Fence terminal transitions for the bounded recovery transaction.
        # No job rows are changed by the broker; rollback releases the fence.
        connection.execute("BEGIN IMMEDIATE")
        _check_job(connection, request, lease, binding=binding)
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _mount_proof(owned: Path, namespace_fd: int) -> dict[str, str] | None:
    """Observe a mount instance, not the current device behind a reusable pathname."""
    try:
        output = _command(
            ("/usr/bin/findmnt", "--json", "--list", "--output",
             "SOURCE,TARGET,ID,MAJ:MIN,FSROOT,FSTYPE"), namespace_fd=namespace_fd,
        ).stdout
    except BrokerError as error:
        raise RecoveryPending from error
    try:
        raw = json.loads(output)
        targets = [m for m in raw["filesystems"] if m.get("target") == str(owned)]
        if len(targets) > 1:
            raise BrokerError("foreign or duplicate recovery view")
        if not targets:
            return None
        mount = targets[0]
        if any(mount.get(key) is None for key in
               ("source", "target", "id", "maj:min", "fsroot", "fstype")):
            raise ValueError("missing mount evidence")
        proof = {key: str(mount[key]) for key in
                 ("source", "target", "id", "maj:min", "fsroot", "fstype")}
        if (not proof["id"].isdecimal() or not re.fullmatch(r"\d+:\d+", proof["maj:min"])
            or not proof["fsroot"].startswith("/") or not proof["fstype"]
            or not BLOCK_DEVICE.fullmatch(proof["source"])):
            raise ValueError("invalid mount evidence")
        namespace = os.fstat(namespace_fd)
        proof["namespace"] = f"{namespace.st_dev}:{namespace.st_ino}"
        proof["boot"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return proof
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RecoveryPending from error


def _retire_disconnected_mount(
    request: dict[str, str], lease: dict[str, str], owned: Path,
    namespace_fd: int, incarnation: str,
) -> None:
    observed = _mount_proof(owned, namespace_fd)
    if observed is None:
        return
    try:
        recorded = json.loads(lease.get("mount_proof", "null"))
    except ValueError as error:
        raise RecoveryPending from error
    if recorded != observed:
        if observed["source"] not in {lease["block_device"], lease.get("recovery_block")}:
            raise BrokerError("foreign or duplicate recovery view")
        # Old leases have no proof; a matching path is never unmount authority.
        raise RecoveryPending
    if lease.get("mount_diskseq") == incarnation:
        if observed["source"] != request["block_device"]:
            raise BrokerError("foreign or duplicate recovery view")
        return
    if not lease.get("mount_diskseq"):
        raise RecoveryPending
    if any(other.get("lease_id") != lease["lease_id"] and (
        other.get("owned_root") == str(owned) or other.get("mount_proof") == lease["mount_proof"]
    ) for other in _load_leases().values()):
        raise BrokerError("foreign or duplicate recovery view")
    # Authority and unique returned identity were checked under the job fence.
    # Ordinary unmount only, in the authenticated consumer namespace.
    try:
        if _mount_proof(owned, namespace_fd) != observed:
            raise RecoveryPending
        _unmount(owned, namespace_fd=namespace_fd)
    except BrokerError as error:
        raise RecoveryPending from error
    if _mount_proof(owned, namespace_fd) is not None:
        raise RecoveryPending


def _remember_mount(
    request: dict[str, str], lease: dict[str, str], owned: Path,
    namespace_fd: int, incarnation: str,
) -> dict[str, str]:
    proof = _mount_proof(owned, namespace_fd)
    if proof is None or proof["source"] != request["block_device"]:
        raise RecoveryPending
    if _diskseq(request["drive_device"]) != incarnation:
        raise RecoveryPending
    current = _lease_for(request)
    if current is None or current.get("lease_id") != lease["lease_id"]:
        raise BrokerError("recovery ownership was revoked")
    _check_lease(request, current)
    updated = {**lease, "mount_proof": json.dumps(proof, sort_keys=True),
               "mount_diskseq": incarnation}
    leases = _load_leases()
    leases[_lease_key(lease)] = updated
    try:
        _save_leases(leases)
    except OSError as error:
        raise RecoveryPending from error
    return updated


def _recovery_view(request: dict[str, str], owned: Path, namespace_fd: int) -> bool:
    try:
        consumer = _mounts(request["block_device"], namespace_fd)
        native = _mounts(request["block_device"])
        # Independently inspect the target: filtering by source alone could
        # hide a foreign mount occupying the intended owned path.
        raw = json.loads(_command(
            ("/usr/bin/findmnt", "--json", "--list", "--output", "SOURCE,TARGET,OPTIONS"),
            namespace_fd=namespace_fd,
        ).stdout)
        targets = [m for m in raw["filesystems"] if m.get("target") == str(owned)]
    except (BrokerError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RecoveryPending from error
    if len(targets) > 1 or (targets and targets[0].get("source") != request["block_device"]):
        raise BrokerError("foreign or duplicate recovery view")
    if owned in consumer and (
        not {"rw", "nodev", "nosuid", "noexec"}.issubset(consumer[owned])
        or "ro" in consumer[owned]
    ):
        raise BrokerError("owned mount has unsafe state")
    return not native and len(consumer) == 1 and _mounted(consumer, owned, "rw")


def _unique_recovery_identity(request: dict[str, str]) -> None:
    raw = json.loads(
        # PATH does not imply a tree; partition identity inherits the disk serial.
        _command(("/usr/bin/lsblk", "--json", "--tree", "--paths", "--output", "PATH,UUID,SERIAL")).stdout
    )
    matches: list[str] = []

    def collect(nodes: list[dict[str, Any]], inherited_serial: str = "") -> None:
        for node in nodes:
            serial = str(node.get("serial") or inherited_serial)
            if node.get("uuid") == request["uuid"] and (
                not request["serial"] or serial == request["serial"]
            ):
                matches.append(str(node.get("path", "")))
            collect(node.get("children", []), serial)

    collect(raw.get("blockdevices", []))
    if matches != [request["block_device"]]:
        raise BrokerError("USB recovery identity is absent or ambiguous")


def _lease_response(lease: dict[str, str]) -> dict[str, Any]:
    return {
        "ok": True,
        **{
            key: lease[key]
            for key in ("owned_root", "lease_id", "generation", "consumer_id", "native_user")
        },
    }


def _recover(request: dict[str, str], lease: dict[str, str], namespace_fd: int) -> dict[str, Any]:
    owned = _check_lease(request, lease)
    if lease.get("consumer_id") != SERVICE_USER:
        raise BrokerError("foreign ownership consumer")
    try:
        with _job_authority(request, lease, namespace_fd) as connection:
            _unique_recovery_identity(request)
            if request["operation"] == "job_lease":
                return _lease_response(lease)
            incarnation = _diskseq(request["drive_device"])
            if not incarnation or not lease.get("diskseq"):
                raise BrokerError("USB disconnection evidence is unavailable")
            _retire_disconnected_mount(request, lease, owned, namespace_fd, incarnation)
            complete = _recovery_view(request, owned, namespace_fd)
            if not complete:
                if incarnation == lease["diskseq"] and not lease.get("recovering"):
                    raise BrokerError("controlled view loss was not a USB disconnection")
                # Persist responsibility BEFORE any action; a crash/lost reply
                # retains the same opaque lease and all affected guard markers.
                lease = {
                    **lease,
                    "guard_blocks": json.dumps(sorted(_lease_guard_blocks(lease) | {request["block_device"]})),
                    "job_id": request["job_id"],
                    "recovering": "1",
                    "recovery_block": request["block_device"],
                }
                leases = _load_leases()
                leases[_lease_key(lease)] = lease
                _save_leases(leases)
                _guard_acquire(request["block_device"])
                if _mounts(request["block_device"]):
                    try:
                        _command(
                            (
                                "/usr/bin/udisksctl",
                                "unmount",
                                "--block-device",
                                request["block_device"],
                                "--no-user-interaction",
                            )
                        )
                    except BrokerError as error:
                        if _mounts(request["block_device"]):
                            raise RecoveryPending from error
                    if _mounts(request["block_device"]):
                        raise RecoveryPending
                if owned not in _mounts(request["block_device"], namespace_fd):
                    if owned.exists() and (not owned.is_dir() or any(owned.iterdir())):
                        raise BrokerError("owned mount path is not empty")
                    owned.mkdir(mode=0o700, parents=False, exist_ok=True)
                    os.chown(
                        owned,
                        pwd.getpwnam(SERVICE_USER).pw_uid,
                        grp.getgrnam("plankton-inference").gr_gid,
                    )
                    if _mount_proof(owned, namespace_fd) is not None:
                        raise BrokerError("foreign or duplicate recovery view")
                    try:
                        _mount_owned(request["block_device"], owned, namespace_fd=namespace_fd)
                    except BrokerError as error:
                        if _mount_proof(owned, namespace_fd) is None:
                            raise RecoveryPending from error
                    lease = _remember_mount(request, lease, owned, namespace_fd, incarnation)
            _identity(request)
            _unique_recovery_identity(request)
            if not _recovery_view(request, owned, namespace_fd):
                raise RecoveryPending
            _check_job(connection, request, lease)
            current = _lease_for(request)
            if current is None or current.get("lease_id") != lease["lease_id"]:
                raise BrokerError("recovery ownership was revoked")
            _check_lease(request, current)
            if current.get("consumer_id") != SERVICE_USER or current.get("job_id") != request["job_id"]:
                raise BrokerError("recovery ownership was revoked")
            if _diskseq(request["drive_device"]) != incarnation:
                raise RecoveryPending
            leases = _load_leases()
            # Replace only this lease; preserve opaque identity and generation.
            leases.pop(_lease_key(lease), None)
            recovered = {
                **lease,
                "block_device": request["block_device"],
                "drive_device": request["drive_device"],
                "diskseq": incarnation,
                "job_id": request["job_id"],
            }
            recovered.pop("recovering", None)
            recovered.pop("recovery_block", None)
            recovered.pop("guard_blocks", None)
            leases[_lease_key(request)] = recovered
            _guard_acquire(request["block_device"])
            _retire_lease_guards(lease, leases)
            _save_leases(leases)
            return _lease_response(recovered)
    except (RecoveryPending, sqlite3.OperationalError) as error:
        raise BrokerError("USB ownership recovery is pending") from error
    except BrokerError as error:
        if lease.get("recovering") and str(error) == "owned mount has unsafe state":
            # A partial RO view is not permission to export, nor evidence that
            # the original owner was revoked. Keep its recovery responsibility.
            raise BrokerError("USB ownership recovery is pending") from error
        raise


def _bind_job(request: dict[str, str], lease: dict[str, str], namespace_fd: int) -> dict[str, Any]:
    owned = _check_lease(request, lease)
    if (request["block_device"] != lease["block_device"]
        or not lease.get("diskseq") or _diskseq(request["drive_device"]) != lease["diskseq"]):
        raise BrokerError("job transfer requires original attached device")
    with _job_authority(request, lease, namespace_fd, binding=True) as connection:
        if not _recovery_view(request, owned, namespace_fd):
            raise BrokerError("job transfer requires current exclusive ownership")
        _check_job(connection, request, lease, binding=True)
        stat = _job_database(namespace_fd).stat()
        bound = {**lease, "job_id": request["job_id"], "database": f"{stat.st_dev}:{stat.st_ino}"}
        proof = _mount_proof(owned, namespace_fd)
        if (proof is None or proof["source"] != request["block_device"]
            or _diskseq(request["drive_device"]) != lease["diskseq"]):
            raise BrokerError("job transfer requires mount provenance")
        bound.update(mount_proof=json.dumps(proof, sort_keys=True), mount_diskseq=lease["diskseq"])
        leases = _load_leases()
        leases[_lease_key(lease)] = bound
        _save_leases(leases)
        return _lease_response(bound)


def handle(request: object, *, namespace_fd: int | None = None) -> dict[str, Any]:
    # The socket server is serial; also fence in-process callers and commit checks.
    with _TRANSITION_LOCK:
        return _handle(request, namespace_fd=namespace_fd)


def _handle(request: object, *, namespace_fd: int | None = None) -> dict[str, Any]:
    values = _validate_request(request)
    _identity(values)
    lease = _lease_for(values)
    if lease is not None and lease.get("cleanup_pending") and values["operation"] != "reconcile":
        raise BrokerError("USB ownership cleanup is pending")
    if values["operation"] in {"recover", "job_lease", "bind_job"}:
        if lease is None or namespace_fd is None:
            raise BrokerError("recovery ownership is unavailable")
        if values["operation"] == "bind_job":
            return _bind_job(values, lease, namespace_fd)
        return _recover(values, lease, namespace_fd)
    if values["operation"] == "claim":
        if lease is not None:
            raise BrokerError("device is already owned")
        label = values["uuid"].replace("/", "_")[:64]
        owned = _validate_owned_path(values["owned_root"] or str(SERVICE_ROOT / label))
        return _claim(values, owned, namespace_fd=namespace_fd)
    if values["operation"] == "reconcile" and namespace_fd is None:
        raise BrokerError("consumer namespace is required")
    if lease is None:
        return {"ok": True, "released": False}
    owned = _check_lease(values, lease)
    if values["operation"] == "reconcile":
        if namespace_fd is not None and lease.get("issued_at"):
            try:
                with _job_authority(values, lease, namespace_fd):
                    # A service restart is not revocation of an accepted job.
                    return {"ok": True, "released": False, "retained": True}
            except BrokerError:
                if lease.get("job_id"):
                    # Terminal jobs use the normal cleanup path; database or
                    # namespace uncertainty must not erase recovery authority.
                    with closing(
                        sqlite3.connect(f"{_job_database(namespace_fd).as_uri()}?mode=ro", uri=True)
                    ) as connection:
                        row = connection.execute(
                            "SELECT state FROM jobs WHERE job_id = ?", (lease["job_id"],)
                        ).fetchone()
                    if row is not None and row[0] not in {
                        "COMPLETED",
                        "FAILED",
                        "SECURITY_BLOCKED",
                        "CANCELLED",
                    }:
                        raise
        return _release(values, owned, lease=lease, namespace_fd=namespace_fd)
    if values["operation"] == "release":
        return _release(values, owned, lease=lease, namespace_fd=namespace_fd)
    return _eject(values, owned, lease=lease, namespace_fd=namespace_fd)


def _peer_credentials(connection: socket.socket) -> tuple[int, int]:
    pid, uid, _gid = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    )
    return int(pid), int(uid)


def _receive_request(connection: socket.socket) -> tuple[bytes, int]:
    fd_size = array.array("i").itemsize
    payload, ancillary, flags, _ = connection.recvmsg(
        MAX_REQUEST_BYTES + 1, socket.CMSG_SPACE(fd_size)
    )
    namespace_fds: list[int] = []
    for level, kind, data in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            continue
        fds = array.array("i")
        usable = len(data) - (len(data) % fd_size)
        fds.frombytes(data[:usable])
        namespace_fds.extend(int(fd) for fd in fds)
    if flags & socket.MSG_CTRUNC or len(namespace_fds) != 1:
        for fd in namespace_fds:
            os.close(fd)
        if flags & socket.MSG_CTRUNC:
            raise BrokerError("mount namespace credential was truncated")
        raise BrokerError("exactly one consumer namespace is required")
    namespace_fd = namespace_fds[0]
    try:
        namespace_link = os.readlink(f"/proc/self/fd/{namespace_fd}")
    except OSError as error:
        os.close(namespace_fd)
        raise BrokerError("invalid consumer namespace") from error
    if not namespace_link.startswith("mnt:["):
        os.close(namespace_fd)
        raise BrokerError("invalid consumer namespace")
    return payload, namespace_fd


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    _reconcile_guard_markers()
    allowed_uids = {0, pwd.getpwnam(SERVICE_USER).pw_uid}
    SOCKET_PATH.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        if not SOCKET_PATH.is_socket():
            raise SystemExit(f"refusing non-socket path: {SOCKET_PATH}")
        SOCKET_PATH.unlink()
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(SOCKET_PATH))
        os.chown(SOCKET_PATH, 0, grp.getgrnam("plankton-inference").gr_gid)
        os.chmod(SOCKET_PATH, 0o660)
        listener.listen(8)
        listener.settimeout(1)
        while not stopping:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                namespace_fd: int | None = None
                try:
                    _peer_pid, peer_uid = _peer_credentials(connection)
                    if peer_uid not in allowed_uids:
                        raise BrokerError("unauthorized mount broker peer")
                    payload, namespace_fd = _receive_request(connection)
                    if len(payload) > MAX_REQUEST_BYTES or not payload.endswith(b"\n"):
                        raise BrokerError("invalid mount broker request")
                    response = handle(json.loads(payload.decode()), namespace_fd=namespace_fd)
                except (BrokerError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    log.warning("request rejected: %s", error)
                    response = {"ok": False, "error": str(error)}
                except Exception:
                    log.exception("request failed")
                    response = {"ok": False, "error": "mount broker operation failed"}
                finally:
                    if namespace_fd is not None:
                        os.close(namespace_fd)
                try:
                    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
                except OSError as error:
                    # Delivery failure cannot revoke a committed transition or stop the listener.
                    log.warning("broker peer transport failed: %s", type(error).__name__)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    serve()
