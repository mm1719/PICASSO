from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image
from plankton_usb_agent import ScannerUnavailable, UsbAgent, UsbAgentError
from plankton_usb_agent.agent import PreflightSummary
from plankton_usb_agent.server import serve

SCANNER_STATUSES = frozenset({"clean", "threat", "unavailable", "stale"})
MODEL_FAULTS = frozenset({"nan", "inf", "error"})
ARCHIVE_FAULTS = frozenset({"write", "verify"})
COPY_CHECKSUM_FAULTS = frozenset({"once", "always"})
DECODER_FAULTS = frozenset({"timeout", "oom"})
EXPORT_FAULTS = frozenset({"error", "interrupt_after_publish"})
SCENARIO_FILESYSTEMS = frozenset(
    {"fat16", "fat32", "exfat", "ntfs", "ext4", "fat12", "vfat-unknown", "btrfs"}
)


@dataclass(frozen=True, slots=True)
class DeviceScenario:
    present: bool
    uuid: str
    serial: str
    filesystem: str
    label: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ScannerScenario:
    status: str
    threat_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FaultScenario:
    archive: str | None
    copy_checksum: str | None
    decoder_by_path: Mapping[str, str]
    export: str | None


@dataclass(frozen=True, slots=True)
class Scenario:
    device: DeviceScenario
    scanner: ScannerScenario
    storage_has_enough_space: bool
    scores_by_path: Mapping[str, tuple[float, ...]]
    model_faults: Mapping[str, str]
    faults: FaultScenario


class SimulatedInterruption(BaseException):
    pass


def _object(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise TypeError(f"scenario {key} must be an object")
    return value


def _safe_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"scenario {field} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"scenario {field} must be a safe relative path")
    return path.as_posix()


def load_scenario(path: Path) -> Scenario:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("simulator scenario is unavailable or invalid") from error
    if not isinstance(payload, dict):
        raise TypeError("simulator scenario must be an object")

    device = _object(payload, "device")
    uuid = device.get("uuid")
    if not isinstance(uuid, str):
        raise TypeError("scenario device.uuid must be a string")
    serial = device.get("serial", "")
    filesystem = device.get("filesystem", "ext4")
    label = device.get("label", "BLACKBOX")
    size_bytes = device.get("size_bytes", 1024**3)
    present = device.get("present", True)
    if not isinstance(serial, str) or not isinstance(label, str):
        raise TypeError("scenario device serial and label must be strings")
    if filesystem not in SCENARIO_FILESYSTEMS:
        raise ValueError("scenario device.filesystem is unsupported")
    if not isinstance(size_bytes, int) or size_bytes <= 0 or not isinstance(present, bool):
        raise ValueError("scenario device size/presence is invalid")

    scanner = _object(payload, "scanner")
    scanner_status = scanner.get("status", "clean")
    if scanner_status not in SCANNER_STATUSES:
        raise ValueError("scenario scanner.status is invalid")
    raw_threats = scanner.get("threat_paths", [])
    if not isinstance(raw_threats, list):
        raise TypeError("scenario scanner.threat_paths must be a list")
    threats = tuple(
        _safe_relative_path(item, "scanner.threat_paths") for item in raw_threats
    )

    raw_scores = payload.get("scores_by_path", {})
    if not isinstance(raw_scores, dict):
        raise TypeError("scenario scores_by_path must be an object")
    scores: dict[str, tuple[float, ...]] = {}
    for raw_path, raw_values in raw_scores.items():
        relative_path = _safe_relative_path(raw_path, "scores_by_path key")
        if not isinstance(raw_values, list) or len(raw_values) != 20:
            raise ValueError("each scenario score vector must contain 20 values")
        values = tuple(float(value) for value in raw_values)
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
            raise ValueError("scenario score vectors must contain finite values in [0, 1]")
        scores[relative_path] = values

    raw_faults = payload.get("model_faults", {})
    if not isinstance(raw_faults, dict):
        raise TypeError("scenario model_faults must be an object")
    faults: dict[str, str] = {}
    for raw_path, fault in raw_faults.items():
        relative_path = _safe_relative_path(raw_path, "model_faults key")
        if fault not in MODEL_FAULTS:
            raise ValueError("scenario model fault is invalid")
        faults[relative_path] = str(fault)

    storage = payload.get("storage_has_enough_space", True)
    if not isinstance(storage, bool):
        raise TypeError("scenario storage_has_enough_space must be boolean")
    raw_public_faults = _object(payload, "faults")
    archive_fault = raw_public_faults.get("archive")
    copy_checksum_fault = raw_public_faults.get("copy_checksum")
    export_fault = raw_public_faults.get("export")
    if archive_fault is not None and archive_fault not in ARCHIVE_FAULTS:
        raise ValueError("scenario faults.archive is invalid")
    if copy_checksum_fault is not None and copy_checksum_fault not in COPY_CHECKSUM_FAULTS:
        raise ValueError("scenario faults.copy_checksum is invalid")
    if export_fault is not None and export_fault not in EXPORT_FAULTS:
        raise ValueError("scenario faults.export is invalid")
    raw_decoder_faults = raw_public_faults.get("decoder_by_path", {})
    if not isinstance(raw_decoder_faults, dict):
        raise TypeError("scenario faults.decoder_by_path must be an object")
    decoder_faults: dict[str, str] = {}
    for raw_path, fault in raw_decoder_faults.items():
        relative_path = _safe_relative_path(raw_path, "faults.decoder_by_path key")
        if fault not in DECODER_FAULTS:
            raise ValueError("scenario decoder fault is invalid")
        decoder_faults[relative_path] = str(fault)
    return Scenario(
        DeviceScenario(present, uuid, serial, str(filesystem), label, size_bytes),
        ScannerScenario(str(scanner_status), threats),
        storage,
        scores,
        faults,
        FaultScenario(
            str(archive_fault) if archive_fault is not None else None,
            str(copy_checksum_fault) if copy_checksum_fault is not None else None,
            decoder_faults,
            str(export_fault) if export_fault is not None else None,
        ),
    )


class OperationJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(
        self,
        component: str,
        operation: str,
        status: str,
        **details: object,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": component,
            "operation": operation,
            "status": status,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_UN)


class SimulatedHost:
    def __init__(
        self,
        mount_root: Path,
        owned_mount_root: Path,
        scenario_path: Path,
        journal: OperationJournal,
    ) -> None:
        self.mount_root = mount_root
        self.owned_mount_root = owned_mount_root
        self.scenario_path = scenario_path
        self.journal = journal
        self.mount_mode: str | None = None
        self.owned_mount_mode: str | None = None

    @staticmethod
    def _result(
        args: tuple[str, ...], return_code: int = 0, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, return_code, stdout, stderr)

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = tuple(argv)
        scenario = load_scenario(self.scenario_path)
        command = args[0]
        self.journal.append("usb_host", command, "called")
        if command == "lsblk":
            blocks: list[dict[str, object]] = []
            if scenario.device.present:
                blocks.append(
                    {
                        "path": "/dev/blackbox",
                        "type": "disk",
                        "rm": True,
                        "tran": "usb",
                        "serial": scenario.device.serial,
                        "size": scenario.device.size_bytes,
                        "children": [
                            {
                                "path": "/dev/blackbox1",
                                "type": "part",
                                "fstype": "vfat"
                                if scenario.device.filesystem
                                in {"fat16", "fat32", "fat12", "vfat-unknown"}
                                else scenario.device.filesystem,
                                "fsver": {
                                    "fat16": "FAT16",
                                    "fat32": "FAT32",
                                    "fat12": "FAT12",
                                    "vfat-unknown": "UNKNOWN",
                                }.get(scenario.device.filesystem),
                                "uuid": scenario.device.uuid,
                                "label": scenario.device.label,
                                "size": scenario.device.size_bytes,
                            }
                        ],
                    }
                )
            return self._result(args, stdout=json.dumps({"blockdevices": blocks}))
        if command == "findmnt":
            if not scenario.device.present or (self.mount_mode is None and self.owned_mount_mode is None):
                return self._result(args, 1)
            filesystems = []
            if self.mount_mode is not None:
                filesystems.append(
                    {
                        "target": str(self.mount_root),
                        "options": f"{self.mount_mode},nodev,nosuid,noexec",
                    }
                )
            if self.owned_mount_mode is not None:
                filesystems.append(
                    {
                        "target": str(self.owned_mount_root),
                        "options": f"{self.owned_mount_mode},nodev,nosuid,noexec",
                    }
                )
            return self._result(
                args,
                stdout=json.dumps({"filesystems": filesystems}),
            )
        if args[:2] == ("udisksctl", "mount"):
            self.mount_mode = args[args.index("--options") + 1].split(",", 1)[0]
            return self._result(args, stdout="mounted")
        if args[:2] == ("udisksctl", "unmount"):
            self.mount_mode = None
            return self._result(args, stdout="unmounted")
        if args[:2] == ("udisksctl", "power-off"):
            return self._result(args, stdout="powered off")
        if command == "clamdscan":
            if scenario.scanner.status == "threat":
                output = "".join(
                    f"{self.mount_root / path}: Blackbox-Test-Signature FOUND\n"
                    for path in scenario.scanner.threat_paths
                )
                return self._result(args, 1, output)
            return self._result(args)
        raise AssertionError(args)


class SimulatedUsbAgent(UsbAgent):
    def __init__(
        self,
        *,
        mount_root: Path,
        job_root: Path,
        scenario_path: Path,
        journal: OperationJournal,
    ) -> None:
        self.scenario_path = scenario_path
        self.operation_journal = journal
        mount_key = hashlib.sha256(str(mount_root).encode()).hexdigest()[:12]
        owned_mount_root = Path("/tmp") / f"plankton-simulated-{mount_key}.controlled"
        if owned_mount_root.exists() or owned_mount_root.is_symlink():
            if not owned_mount_root.is_symlink() or owned_mount_root.resolve() != mount_root.resolve():
                raise UsbAgentError("simulated controlled mount path is invalid")
        else:
            owned_mount_root.symlink_to(mount_root, target_is_directory=True)
        self._simulated_host = SimulatedHost(
            mount_root, owned_mount_root, scenario_path, journal
        )
        self._simulated_mount_root = owned_mount_root
        super().__init__(
            job_root=job_root,
            reserve_bytes=0,
            run_command=self._simulated_host,
            mount_broker_request=self._simulated_broker,
            ownership_root_parent=None,
        )
        self._simulated_host.mount_mode = "rw"

    def _simulated_broker(self, payload: dict[str, str]) -> dict[str, object]:
        operation = payload["operation"]
        if operation == "claim":
            self._simulated_host.mount_mode = None
            self._simulated_host.owned_mount_mode = "rw"
        elif operation == "release":
            self._simulated_host.owned_mount_mode = None
            self._simulated_host.mount_mode = "rw"
        elif operation == "eject":
            self._simulated_host.owned_mount_mode = None
            self._simulated_host.mount_mode = None
        return {
            "ok": True,
            "owned_root": str(self._simulated_mount_root),
            "lease_id": "simulated-lease",
            "generation": "1",
            "consumer_id": "plankton-usb",
            "released": operation != "claim",
        }

    def _mount_as_user(self, device: object, user: str, *, writable: bool = True) -> Path:
        del device, user
        self._simulated_host.mount_mode = "rw" if writable else "ro"
        return self._simulated_host.mount_root

    def journal_operation(self, operation: str, status: str, error: str | None = None) -> None:
        details = {"error": error} if error else {}
        self.operation_journal.append("usb", operation, status, **details)

    def _copy_is_valid(
        self,
        temporary: Path,
        expected_size: int,
        expected_hash: str,
        attempt: int,
    ) -> bool:
        fault = load_scenario(self.scenario_path).faults.copy_checksum
        if fault == "always" or (fault == "once" and attempt == 0):
            self.operation_journal.append(
                "fault",
                "copy_checksum",
                "injected",
                attempt=attempt + 1,
            )
            return False
        return super()._copy_is_valid(temporary, expected_size, expected_hash, attempt)

    def _validate_signatures(self) -> None:
        status = load_scenario(self.scenario_path).scanner.status
        if status == "unavailable":
            raise ScannerUnavailable("simulated scanner is unavailable")
        if status == "stale":
            raise ScannerUnavailable("simulated scanner signatures are stale")

    def preflight_selected_tree(
        self,
        device_id: str,
        selected_relative_path: str,
        metadata_relative_path: str | None = None,
    ) -> PreflightSummary:
        summary = super().preflight_selected_tree(
            device_id, selected_relative_path, metadata_relative_path
        )
        if load_scenario(self.scenario_path).storage_has_enough_space:
            return summary
        return PreflightSummary(
            summary.file_count,
            summary.source_bytes,
            summary.required_free_bytes,
            max(0, summary.required_free_bytes - 1),
            False,
        )

    def export_results(
        self,
        device_id: str,
        selected_relative_path: str,
        output_directory: Path,
    ) -> str:
        if load_scenario(self.scenario_path).faults.export == "error":
            self.operation_journal.append("fault", "export", "injected")
            raise UsbAgentError("simulated export failure")
        return super().export_results(device_id, selected_relative_path, output_directory)


class DeterministicInferenceModel:
    def __init__(self, scenario_path: Path, journal: OperationJournal) -> None:
        self.scenario_path = scenario_path
        self.journal = journal
        self.journal.append("model", "load", "completed")

    @staticmethod
    def _default_scores(relative_path: str) -> tuple[float, ...]:
        class_index = hashlib.sha256(relative_path.encode()).digest()[0] % 20
        scores = [0.01] * 20
        scores[class_index] = 0.81
        return tuple(scores)

    def predict_paths(self, relative_paths: Sequence[str]) -> dict[str, tuple[float, ...]]:
        scenario = load_scenario(self.scenario_path)
        results: dict[str, tuple[float, ...]] = {}
        for relative_path in relative_paths:
            fault = scenario.model_faults.get(relative_path)
            if fault == "error":
                self.journal.append("model", "predict", "failed", path=relative_path)
                raise RuntimeError("simulated inference failure")
            scores = list(
                scenario.scores_by_path.get(relative_path, self._default_scores(relative_path))
            )
            if fault == "nan":
                scores[0] = float("nan")
            elif fault == "inf":
                scores[0] = float("inf")
            results[relative_path] = tuple(scores)
        self.journal.append(
            "model", "predict", "completed", paths=list(relative_paths), count=len(relative_paths)
        )
        return results


def _simulator_paths() -> tuple[Path, OperationJournal]:
    scenario = os.environ.get("PLANKTON_SIMULATOR_CONFIG")
    journal = os.environ.get("PLANKTON_SIMULATOR_JOURNAL")
    if not scenario or not journal:
        raise RuntimeError("simulator requires config and journal paths")
    return Path(scenario), OperationJournal(Path(journal))


def simulated_model_factory(_source_root: Path) -> DeterministicInferenceModel:
    scenario, journal = _simulator_paths()
    return DeterministicInferenceModel(scenario, journal)


def simulated_fault_injector(point: str, relative_path: str | None) -> None:
    scenario_path, journal = _simulator_paths()
    faults = load_scenario(scenario_path).faults
    if point == "decode" and relative_path is not None:
        fault = faults.decoder_by_path.get(relative_path)
        if fault is None:
            return
        journal.append("fault", f"decoder_{fault}", "injected", path=relative_path)
        if fault == "timeout":
            raise TimeoutError("simulated decoder timeout")
        raise MemoryError("simulated decoder OOM")
    if point == "archive_write" and faults.archive == "write":
        journal.append("fault", "archive_write", "injected")
        raise OSError("simulated archive write failure")
    if point == "archive_verify" and faults.archive == "verify":
        journal.append("fault", "archive_verify", "injected")
        raise RuntimeError("simulated archive verification failure")
    if point == "export_after_publish" and faults.export == "interrupt_after_publish":
        journal.append("fault", "export_after_publish", "injected")
        raise SimulatedInterruption("simulated interruption after export publish")


def initialize_fixture(usb_root: Path) -> None:
    source = usb_root / "science"
    nested = source / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    high = source / "high.png"
    low = nested / "low.png"
    if not high.exists():
        Image.new("RGB", (16, 16), "green").save(high)
    if not low.exists():
        Image.new("RGBA", (21, 13), "blue").save(low)
    metadata = source / "metadata.csv"
    if not metadata.exists():
        metadata.write_text(
            "source_relative_path,image_id,note\n"
            "high.png,BB-0001,high score\n"
            "nested/low.png,BB-0002,low score\n",
            encoding="utf-8",
        )


def serve_simulator(
    socket_path: Path,
    usb_root: Path,
    job_root: Path,
    scenario_path: Path,
    journal_path: Path,
) -> None:
    usb_root.mkdir(parents=True, exist_ok=True)
    job_root.mkdir(parents=True, exist_ok=True)
    load_scenario(scenario_path)
    serve(
        socket_path,
        SimulatedUsbAgent(
            mount_root=usb_root,
            job_root=job_root,
            scenario_path=scenario_path,
            journal=OperationJournal(journal_path),
        ),
    )
