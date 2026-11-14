from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import plankton_usb_agent.agent as agent_module
import pytest
from plankton_usb_agent import UsbAgent, UsbAgentError
from plankton_usb_agent.server import dispatch


class Host:
    def __init__(self, root: Path, on_scan: Callable[[], None] | None = None) -> None:
        self.root = root
        self.on_scan = on_scan
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = tuple(argv)
        self.calls.append(args)
        if args[0] == "lsblk":
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"blockdevices": [{"path": "/dev/fake", "type": "disk", "rm": True, "tran": "usb", "serial": "s", "children": [{"path": "/dev/fake1", "type": "part", "fstype": "ext4", "fsver": "1", "uuid": "u", "size": 1}]}]}), ""
            )
        if args[0] == "findmnt":
            return subprocess.CompletedProcess(args, 0, json.dumps({"filesystems": [{"target": str(self.root), "options": "ro,nodev,nosuid,noexec"}]}), "")
        if args[0] == "clamdscan":
            if self.on_scan is not None:
                self.on_scan()
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def test_scan_and_import_scans_once_and_reports_bytes(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    jobs = tmp_path / "jobs"
    signatures = tmp_path / "clamav"
    source = root / "science"
    source.mkdir(parents=True)
    jobs.mkdir()
    signatures.mkdir()
    (signatures / "daily.cvd").write_bytes(b"current")
    (source / "a.jpg").write_bytes(b"a")
    (source / "metadata.csv").write_text("image_id\na\n", encoding="utf-8")
    host = Host(root)
    agent = UsbAgent(job_root=jobs, signature_directory=signatures, reserve_bytes=0, run_command=host)
    device_id = agent.list_devices()[0].device_id
    events: list[tuple[object, ...]] = []

    result = dispatch(
        agent,
        {"operation": "scan_and_import", "args": {"device_id": device_id, "selected_relative_path": "science", "staging_directory": str(jobs / "staging"), "metadata_relative_path": "science/metadata.csv"}},
        lambda *values: events.append(values),
    )

    assert result == {"entry_count": 2}
    scan_calls = [call for call in host.calls if call[0] == "clamdscan"]
    assert len(scan_calls) == 1
    assert scan_calls[0][1] == "--stream"
    assert events[0][0] == "scanning"
    assert events[-1][:3] == ("importing", 1, 1)
    assert events[-1][3] == 12
    assert (jobs / "staging" / "a.jpg").read_bytes() == b"a"


def test_scan_cannot_be_used_to_bypass_atomic_copy(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    jobs = tmp_path / "jobs"
    root.mkdir()
    jobs.mkdir()
    agent = UsbAgent(job_root=jobs)
    with pytest.raises(UsbAgentError, match="unsupported argument"):
        dispatch(agent, {"operation": "scan_and_import", "args": {"skip_scan": True}})


def test_scan_and_import_rejects_same_metadata_content_replacement(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    jobs = tmp_path / "jobs"
    signatures = tmp_path / "clamav"
    source = root / "science"
    source.mkdir(parents=True)
    jobs.mkdir()
    signatures.mkdir()
    (signatures / "daily.cvd").write_bytes(b"current")
    image = source / "same.jpg"
    image.write_bytes(b"safe")
    original = image.stat()

    def replace_after_scan() -> None:
        image.write_bytes(b"evil")
        os.utime(image, ns=(original.st_atime_ns, original.st_mtime_ns))

    host = Host(root, replace_after_scan)
    agent = UsbAgent(
        job_root=jobs,
        signature_directory=signatures,
        reserve_bytes=0,
        run_command=host,
    )
    device_id = agent.list_devices()[0].device_id

    with pytest.raises(UsbAgentError, match="source changed after malware scan"):
        dispatch(
            agent,
            {
                "operation": "scan_and_import",
                "args": {
                    "device_id": device_id,
                    "selected_relative_path": "science",
                    "staging_directory": str(jobs / "staging"),
                },
            },
        )

    assert image.stat().st_size == original.st_size
    assert image.stat().st_mtime_ns == original.st_mtime_ns
    assert not (jobs / "staging").exists()


def test_scan_and_import_treats_hostile_filename_as_one_argv_value(tmp_path: Path) -> None:
    root = tmp_path / "usb"
    jobs = tmp_path / "jobs"
    signatures = tmp_path / "clamav"
    source = root / "science"
    source.mkdir(parents=True)
    jobs.mkdir()
    signatures.mkdir()
    (signatures / "daily.cvd").write_bytes(b"current")
    name = "--:$(touch PWNED)\n浮游生物.jpg"
    image = source / name
    image.write_bytes(b"safe")
    host = Host(root)
    agent = UsbAgent(
        job_root=jobs,
        signature_directory=signatures,
        reserve_bytes=0,
        run_command=host,
    )
    device_id = agent.list_devices()[0].device_id

    dispatch(
        agent,
        {
            "operation": "scan_and_import",
            "args": {
                "device_id": device_id,
                "selected_relative_path": "science",
                "staging_directory": str(jobs / "staging"),
            },
        },
    )

    [scan] = [call for call in host.calls if call[0] == "clamdscan"]
    assert scan[-2:] == ("--", str(image))
    assert (jobs / "staging" / name).read_bytes() == b"safe"
    assert not (tmp_path / "PWNED").exists()


def test_scan_and_import_reports_heartbeat_while_snapshot_is_slow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "usb"
    jobs = tmp_path / "jobs"
    signatures = tmp_path / "clamav"
    source = root / "science"
    source.mkdir(parents=True)
    jobs.mkdir()
    signatures.mkdir()
    (signatures / "daily.cvd").write_bytes(b"current")
    (source / "slow.jpg").write_bytes(b"safe")
    host = Host(root)
    agent = UsbAgent(
        job_root=jobs,
        signature_directory=signatures,
        reserve_bytes=0,
        run_command=host,
    )
    device_id = agent.list_devices()[0].device_id
    hash_started = threading.Event()
    release_hash = threading.Event()
    progress_seen = threading.Event()
    real_sha256 = agent_module._sha256

    def slow_sha256(path: Path) -> str:
        if path == source / "slow.jpg" and not hash_started.is_set():
            hash_started.set()
            release_hash.wait(10)
        return real_sha256(path)

    monkeypatch.setattr(agent_module, "_sha256", slow_sha256)
    request = {
        "operation": "scan_and_import",
        "args": {
            "device_id": device_id,
            "selected_relative_path": "science",
            "staging_directory": str(jobs / "staging"),
        },
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            dispatch,
            agent,
            request,
            lambda *_values: progress_seen.set(),
        )
        assert hash_started.wait(1)
        heartbeat_met_deadline = progress_seen.wait(5.1)
        release_hash.set()
        future.result(timeout=5)

    assert heartbeat_met_deadline, "snapshot hashing was silent for more than five seconds"
