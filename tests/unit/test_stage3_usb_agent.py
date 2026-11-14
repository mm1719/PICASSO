from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from socketserver import BaseServer
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from plankton_usb_agent import ScannerUnavailable, SecurityBlocked, UsbAgent, UsbAgentError
from plankton_usb_agent.agent import LEGACY_OUTPUT_FILES, OUTPUT_FILES
from plankton_usb_agent.ownership import OwnershipError, OwnershipLease
from plankton_usb_agent.server import _Handler, dispatch

from host import usb_mount_broker


class FakeHost:
    def __init__(self, mount_root: Path) -> None:
        self.mount_root = mount_root
        self.mount_mode: str | None = None
        self.service_mount_mode: str | None = None
        self.service_mount_target: str | None = None
        self.preexisting_mount_target: str | None = None
        self.hide_native_mount = False
        self.mount_records: list[tuple[str, str]] | None = None
        self.serial = "USB-SERIAL-001"
        self.uuid = "FS-UUID-001"
        self.label = "PRIVATE-LABEL"
        self.filesystem = "ext4"
        self.filesystem_version = "1.0"
        self.scan_return_code = 0
        self.scan_stdout = ""
        self.mount_failures_remaining = 0
        self.calls: list[tuple[str, ...]] = []
        self.broker_calls: list[dict[str, str]] = []
        self.lsblk_payload: dict[str, object] | None = None

    def broker(self, payload: dict[str, str]) -> dict[str, object]:
        self.broker_calls.append(payload)
        operation = payload["operation"]
        owned = str(self.mount_root)
        if self.mount_records is not None:
            if operation in {"claim", "recover"}:
                self.mount_records[:] = [(owned, "rw,nodev,nosuid,noexec")]
            elif operation in {"release", "eject"}:
                self.mount_records.clear()
            return {
                "ok": True,
                "owned_root": owned,
                "lease_id": "lease-test",
                "generation": "1",
                "consumer_id": "plankton-usb",
                "native_user": "operator",
                "released": operation != "claim",
            }
        if operation in {"claim", "recover"}:
            self.mount_mode = None
            self.service_mount_mode = "rw"
            self.service_mount_target = owned
        elif operation == "release":
            self.service_mount_mode = None
            self.service_mount_target = None
        elif operation == "eject":
            self.mount_mode = None
            self.service_mount_mode = None
            self.service_mount_target = None
        return {
            "ok": True,
            "owned_root": owned,
            "lease_id": "lease-test",
            "generation": "1",
            "consumer_id": "plankton-usb",
            "native_user": "operator",
            "released": operation != "claim",
        }

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = tuple(argv)
        self.calls.append(args)
        if args[0] == "lsblk":
            payload: dict[str, object] = self.lsblk_payload or {
                "blockdevices": [
                    {
                        "path": "/dev/fake",
                        "type": "disk",
                        "rm": True,
                        "tran": "usb",
                        "serial": self.serial,
                        "size": 4096,
                        "children": [
                            {
                                "path": "/dev/fake1",
                                "type": "part",
                                "fstype": self.filesystem,
                                "fsver": self.filesystem_version,
                                "uuid": self.uuid,
                                "label": self.label,
                                "size": 4096,
                            },
                            {
                                "path": "/dev/fake2",
                                "type": "part",
                                "fstype": "vfat",
                                "uuid": "IGNORED",
                                "size": 4096,
                            },
                        ],
                    }
                ]
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args[0] == "findmnt":
            if self.mount_records is not None:
                if not self.mount_records:
                    return subprocess.CompletedProcess(args, 1, "", "")
                return subprocess.CompletedProcess(
                    args,
                    0,
                    json.dumps({
                        "filesystems": [
                            {"target": target, "options": options}
                            for target, options in self.mount_records
                        ]
                    }),
                    "",
                )
            if self.mount_mode is None and self.service_mount_mode is None:
                return subprocess.CompletedProcess(args, 1, "", "")
            target = self.preexisting_mount_target or str(self.mount_root)
            filesystems = []
            if not self.hide_native_mount and self.mount_mode is not None:
                filesystems.append({
                    "target": target,
                    "options": f"{self.mount_mode},nodev,nosuid,noexec",
                })
            if self.service_mount_mode is not None and self.service_mount_target is not None:
                filesystems.append({
                    "target": self.service_mount_target,
                    "options": f"{self.service_mount_mode},nodev,nosuid,noexec",
                })
            payload = {"filesystems": filesystems}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args[:2] == ("udisksctl", "mount"):
            if self.mount_failures_remaining:
                self.mount_failures_remaining -= 1
                return subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    "Error mounting /dev/fake1: already mounted or mount point busy",
                )
            options = args[args.index("--options") + 1]
            if self.mount_records is not None:
                self.mount_records.append(
                    ("/media/plankton-usb/PRIVATE-LABEL", options)
                )
                return subprocess.CompletedProcess(args, 0, "mounted", "")
            self.mount_mode = options.split(",", 1)[0]
            return subprocess.CompletedProcess(args, 0, "mounted", "")
        if args[:2] == ("udisksctl", "unmount"):
            if self.mount_records is not None:
                self.mount_records.clear()
                return subprocess.CompletedProcess(args, 0, "unmounted", "")
            self.mount_mode = None
            self.service_mount_mode = None
            self.service_mount_target = None
            self.preexisting_mount_target = None
            return subprocess.CompletedProcess(args, 0, "unmounted", "")
        if args[:2] == ("udisksctl", "power-off"):
            return subprocess.CompletedProcess(args, 0, "powered off", "")
        if args[0] == "/usr/bin/gdbus":
            if self.mount_records is not None:
                self.mount_records.append(
                    ("/media/operator/PRIVATE-LABEL", "rw,nodev,nosuid,noexec")
                )
                return subprocess.CompletedProcess(args, 0, "('/media/operator/PRIVATE-LABEL',)\n", "")
            self.mount_mode = "rw"
            self.preexisting_mount_target = "/media/operator/PLANKTON_DEMO_US"
            return subprocess.CompletedProcess(args, 0, "('/media/operator/PLANKTON_DEMO_US',)\n", "")
        if args[0] == "clamdscan":
            return subprocess.CompletedProcess(args, self.scan_return_code, self.scan_stdout, "")
        if args[0] == "/usr/bin/nautilus":
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def _agent(
    tmp_path: Path, *, copy_progress_images: int = 1000
) -> tuple[UsbAgent, FakeHost, Path, Path]:
    mount_root = tmp_path / "usb"
    job_root = tmp_path / "jobs"
    signatures = tmp_path / "clamav"
    mount_root.mkdir()
    job_root.mkdir()
    signatures.mkdir()
    (signatures / "daily.cvd").write_bytes(b"current")
    host = FakeHost(mount_root)
    host.mount_mode = "rw"
    return (
        UsbAgent(
            job_root=job_root,
            signature_directory=signatures,
            reserve_bytes=0,
            copy_progress_images=copy_progress_images,
            run_command=host,
            mount_broker_request=host.broker,
            ownership_root_parent=None,
        ),
        host,
        mount_root,
        job_root,
    )


def test_restart_reconciles_persisted_lease_before_serving(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    device = agent.list_devices()[0]
    host.mount_mode = None
    broker_calls: list[dict[str, str]] = []

    def broker(payload: dict[str, str]) -> dict[str, object]:
        broker_calls.append(payload)
        assert payload["operation"] == "reconcile"
        host.mount_mode = "rw"
        return {"ok": True, "released": True}

    agent.mount_broker_request = broker

    agent.reconcile_persisted_ownership()

    assert broker_calls == [
        {
            "operation": "reconcile",
            "block_device": device.block_device,
            "drive_device": device.drive_device,
            "uuid": device.uuid,
            "serial": device.serial,
            "owned_root": "",
            "consumer_id": "plankton-usb",
        }
    ]
    assert host.mount_mode == "rw"


def test_socket_handler_reports_permission_failure_without_private_path() -> None:
    class PermissionDeniedAgent:
        def journal_operation(self, operation: str, outcome: str, detail: str = "") -> None:
            pass

        def list_devices(self) -> list[object]:
            raise PermissionError("/var/lib/plankton-inference/job-1/outputs")

        def __getattr__(self, name: str) -> object:
            return lambda *args, **kwargs: None

    handler = object.__new__(_Handler)
    handler.rfile = BytesIO(b'{"operation":"list_devices"}\n')
    handler.wfile = BytesIO()
    handler.server = cast(BaseServer, SimpleNamespace(agent=PermissionDeniedAgent()))

    handler.handle()

    response = json.loads(handler.wfile.getvalue())
    assert response == {
        "ok": False,
        "error": "host_filesystem_permission_denied",
        "message": "host filesystem permission denied",
    }


def test_count_summary_bundle_exports_and_retries_without_touching_history(tmp_path: Path) -> None:
    agent, _host, usb, jobs = _agent(tmp_path)
    source = usb / "science"
    source.mkdir()
    historical = source / "inference_results_001"
    historical.mkdir()
    (historical / "README.md").write_bytes(b"historical results; do not rewrite")
    before = {p.name: p.read_bytes() for p in historical.iterdir()}
    outputs = jobs / "counts-job" / "outputs"
    outputs.mkdir(parents=True)
    names = {
        "counts_summary.csv", "job_summary.json", "predictions_top1.csv",
        "low_confidence_top3.csv", "rejected_images.csv",
    }
    for name in names:
        (outputs / name).write_text(f"{name}\n", encoding="utf-8")
    device = agent.list_devices()[0]
    agent.claim(device.device_id)

    exported = agent.export_results(device.device_id, "science", outputs)

    assert exported == "science/inference_results_002"
    assert {p.name: p.read_bytes() for p in (usb / exported).iterdir()} == {
        p.name: p.read_bytes() for p in outputs.iterdir()
    }
    assert agent.export_results(device.device_id, "science", outputs) == exported
    assert not (source / "inference_results_003").exists()
    assert {p.name: p.read_bytes() for p in historical.iterdir()} == before


def test_fake_host_import_export_and_eject(tmp_path: Path) -> None:
    agent, host, usb, jobs = _agent(tmp_path)
    source = usb / "science"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"jpeg-a")
    (source / "nested").mkdir()
    (source / "nested" / "b.PNG").write_bytes(b"png-b")
    (source / "metadata.csv").write_text("image_id,note\na,hello\n", encoding="utf-8")
    (source / "never-run.sh").write_text("exit 1\n", encoding="utf-8")

    devices = agent.list_devices()
    assert len(devices) == 1
    device = devices[0]
    assert device.filesystem == "ext4"
    assert device.serial == "USB-SERIAL-001"
    assert [entry.name for entry in agent.list_directory(device.device_id, "science")] == [
        "nested",
        "a.jpg",
        "metadata.csv",
        "never-run.sh",
    ]
    assert agent.claim(device.device_id) == str(usb)
    with pytest.raises(UsbAgentError, match="exclusively owned"):
        agent.list_directory(device.device_id, "science")

    staging = jobs / "job-1" / "source"
    staging.parent.mkdir()
    manifest = agent.copy_to_staging(
        device.device_id,
        "science",
        staging,
        "science/metadata.csv",
    )
    assert [entry.relative_path for entry in manifest.entries] == [
        "a.jpg",
        "metadata.csv",
        "nested/b.PNG",
    ]
    assert not (staging / "never-run.sh").exists()
    saved_manifest = json.loads((staging / "import_manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest["device_uuid"] == "FS-UUID-001"
    assert (staging / "nested" / "b.PNG").read_bytes() == b"png-b"

    outputs = jobs / "job-1" / "outputs"
    outputs.mkdir()
    for name in OUTPUT_FILES:
        (outputs / name).write_text(f"{name}\n", encoding="utf-8")
    (source / "inference_results_001").mkdir()
    result_path = agent.export_results(device.device_id, "science", outputs)
    assert result_path == "science/inference_results_002"
    assert sorted(path.name for path in (usb / result_path).iterdir()) == sorted(OUTPUT_FILES)
    assert agent.export_results(device.device_id, "science", outputs) == result_path
    assert not (source / "inference_results_003").exists()

    calls_before_flush = len(host.calls)
    agent.flush(device.device_id)
    assert host.service_mount_mode == "rw"
    assert not any(
        call[1] == "unmount"
        for call in host.calls[calls_before_flush:]
        if len(call) > 1
    )

    agent.eject(device.device_id)
    assert host.mount_mode is None
    assert (
        "udisksctl",
        "power-off",
        "--block-device",
        "/dev/fake",
        "--no-user-interaction",
    ) in host.calls


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("failure", ["copy", "verify", "publish", "after_publish"])
def test_bundle_export_failure_and_resume(
    tmp_path: Path, legacy: bool, failure: str
) -> None:
    agent, _, usb, jobs = _agent(tmp_path)
    selected = usb / "science"
    selected.mkdir()
    outputs = jobs / "outputs"
    outputs.mkdir()
    names = LEGACY_OUTPUT_FILES if legacy else OUTPUT_FILES
    for name in names:
        (outputs / name).write_text(name, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in outputs.iterdir()}
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)
    if failure == "copy":
        fault = patch.object(agent, "_copy_verified", side_effect=OSError("copy failed"))
    elif failure == "verify":
        fault = patch.object(agent, "_copy_is_valid", return_value=False)
    elif failure == "publish":
        fault = patch("plankton_usb_agent.agent.os.replace", side_effect=OSError("publish failed"))
    else:
        from plankton_usb_agent.agent import _fsync_directory

        def fail_after_publish(path: Path) -> None:
            if path == selected:
                raise OSError("interrupted after publish")
            _fsync_directory(path)

        fault = patch("plankton_usb_agent.agent._fsync_directory", side_effect=fail_after_publish)
    with fault, pytest.raises((OSError, UsbAgentError)):
        agent.export_results(device_id, "science", outputs)
    assert not list(selected.glob(".*.tmp"))
    assert len(list(selected.iterdir())) == (1 if failure == "after_publish" else 0)
    result = agent.export_results(device_id, "science", outputs)
    assert result == "science/inference_results_001"
    assert agent.export_results(device_id, "science", outputs) == result
    assert {p.name: p.read_bytes() for p in (usb / result).iterdir()} == before
    assert {p.name: p.read_bytes() for p in outputs.iterdir()} == before


@pytest.mark.parametrize("invalid", ["extra", "missing", "symlink", "mixed"])
def test_export_rejects_non_contract_bundle(tmp_path: Path, invalid: str) -> None:
    agent, _, usb, jobs = _agent(tmp_path)
    (usb / "science").mkdir()
    outputs = jobs / "outputs"
    outputs.mkdir()
    for name in OUTPUT_FILES:
        (outputs / name).write_text(name, encoding="utf-8")
    if invalid in {"extra", "mixed"}:
        (outputs / ("README.md" if invalid == "mixed" else "arbitrary.txt")).write_text("extra")
    else:
        (outputs / "counts_summary.csv").unlink()
        if invalid == "symlink":
            (outputs / "counts_summary.csv").symlink_to(outputs / "predictions_top1.csv")
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)
    with pytest.raises(UsbAgentError):
        agent.export_results(device_id, "science", outputs)
    assert list((usb / "science").iterdir()) == []


def test_native_rw_browse_accepts_desktop_mount_without_noexec(tmp_path: Path) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    source = usb / "science"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"jpeg-a")
    host.mount_records = [(str(usb), "rw,nodev,nosuid,relatime,errors=remount-ro")]

    device_id = agent.list_devices()[0].device_id

    assert [entry.name for entry in agent.list_directory(device_id, "science")] == ["a.jpg"]


@pytest.mark.parametrize("missing_option", ("nodev", "nosuid"))
def test_native_browse_rejects_mount_without_nodev_or_nosuid(
    tmp_path: Path, missing_option: str
) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    source = usb / "science"
    source.mkdir()
    options = {"rw", "nodev", "nosuid", "relatime", "errors=remount-ro"}
    options.remove(missing_option)
    host.mount_records = [(str(usb), ",".join(sorted(options)))]
    device_id = agent.list_devices()[0].device_id

    with pytest.raises(UsbAgentError, match="USB is not mounted"):
        agent.list_directory(device_id, "science")


def test_host_has_no_file_manager_operation(tmp_path: Path) -> None:
    agent, _, _, _ = _agent(tmp_path)
    with pytest.raises(UsbAgentError, match="unsupported"):
        dispatch(agent, {"operation": "open_selected_folder", "args": {}})


def test_terminal_handoff_releases_owned_view_and_restores_native_rw(tmp_path: Path) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    device_id = agent.list_devices()[0].device_id
    assert agent.claim(device_id) == str(usb)

    agent.release(device_id)

    assert [call["operation"] for call in host.broker_calls] == ["claim", "release"]
    assert host.service_mount_mode is None
    assert host.mount_mode == "rw"
    assert host.preexisting_mount_target == "/media/operator/PLANKTON_DEMO_US"


def test_claim_is_exclusive_and_does_not_keep_a_native_desktop_view(tmp_path: Path) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    host.mount_records = [(str(usb), "rw,nodev,nosuid,noexec")]
    device_id = agent.list_devices()[0].device_id

    assert agent.claim(device_id) == str(usb)

    assert host.mount_records == [(str(usb), "rw,nodev,nosuid,noexec")]
    assert host.broker_calls[-1]["operation"] == "claim"
    with pytest.raises(UsbAgentError, match="exclusively owned"):
        agent.list_directory(device_id)


def test_claim_closes_only_nautilus_once_after_busy_then_retries(tmp_path: Path) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    device = agent.list_devices()[0]
    lease = OwnershipLease(
        device_id=device.device_id,
        filesystem_uuid=device.uuid,
        hardware_serial=device.serial,
        consumer_id="plankton-usb",
        owned_root=usb,
        lease_id="lease-test",
        generation=1,
    )

    with patch.object(
        agent._ownership,
        "claim",
        side_effect=[OwnershipError("native mount is busy"), lease],
    ) as claim:
        assert agent.claim(device.device_id) == str(usb)

    assert claim.call_count == 2
    assert host.calls.count(("/usr/bin/nautilus", "--quit")) == 1


def test_claim_does_not_close_processes_for_a_non_busy_failure(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    device_id = agent.list_devices()[0].device_id

    with (
        patch.object(
            agent._ownership,
            "claim",
            side_effect=OwnershipError("device identity changed"),
        ),
        pytest.raises(UsbAgentError, match="device identity changed"),
    ):
        agent.claim(device_id)

    assert all(call[0] != "/usr/bin/nautilus" for call in host.calls)


def test_claim_refuses_after_one_nautilus_retry(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    device_id = agent.list_devices()[0].device_id

    with (
        patch.object(
            agent._ownership,
            "claim",
            side_effect=OwnershipError("native mount is busy"),
        ) as claim,
        pytest.raises(UsbAgentError, match="native mount is busy"),
    ):
        agent.claim(device_id)

    assert claim.call_count == 2
    assert host.calls.count(("/usr/bin/nautilus", "--quit")) == 1


def test_release_readiness_rejects_stale_consumer_mount(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    device = agent.list_devices()[0]
    owned = Path("/media/plankton-usb/PRIVATE-LABEL")
    lease = OwnershipLease(
        device_id=device.device_id,
        filesystem_uuid=device.uuid,
        hardware_serial=device.serial,
        consumer_id="plankton-usb",
        owned_root=owned,
        lease_id="lease-test",
        generation=1,
    )

    host.mount_records = [(str(owned), "rw,nodev,nosuid,noexec")]
    assert not agent._verify_released_view(device, lease)
    host.mount_records.clear()
    assert agent._verify_released_view(device, lease)


def test_owned_view_uses_exact_target_when_source_lookup_is_empty(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    device = agent.list_devices()[0]
    owned = Path("/media/plankton-usb/PRIVATE-LABEL")
    lease = OwnershipLease(
        device_id=device.device_id,
        filesystem_uuid=device.uuid,
        hardware_serial=device.serial,
        consumer_id="plankton-usb",
        owned_root=owned,
        lease_id="lease-test",
        generation=1,
    )

    def command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = tuple(argv)
        if args[:4] == ("findmnt", "--json", "--source", device.block_device):
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[:4] == ("findmnt", "--json", "--target", str(owned)):
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({
                    "filesystems": [{
                        "source": device.block_device,
                        "target": str(owned),
                        "options": "rw,nodev,nosuid,noexec",
                    }]
                }),
                "",
            )
        return host(args)

    agent.run_command = command
    assert agent._verify_owned_view(device, lease)


def test_broker_identity_uses_host_compatible_lsblk_device_operand() -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--device" in argv:
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "/usr/bin/lsblk: unrecognized option '--device'\n",
            )
        assert argv[-1] == "/dev/fake1"
        return subprocess.CompletedProcess(argv, 0, "FS-UUID-001 USB-SERIAL-001\n", "")

    with (
        patch.object(usb_mount_broker, "_run", side_effect=run),
        patch.object(usb_mount_broker, "_claim", return_value={"ok": True}),
    ):
        result = usb_mount_broker.handle(
            {
                "operation": "claim",
                "block_device": "/dev/fake1",
                "drive_device": "/dev/fake",
                "uuid": "FS-UUID-001",
                "serial": "USB-SERIAL-001",
                "consumer_id": "plankton-usb",
            }
        )

    assert result == {"ok": True}
    assert len(calls) == 1


def test_broker_derives_one_owned_path_from_the_filesystem_uuid() -> None:
    observed: list[tuple[Path, int | None]] = []

    def claim(
        _request: dict[str, str], owned_root: Path, *, namespace_fd: int | None = None
    ) -> dict[str, object]:
        observed.append((owned_root, namespace_fd))
        return {"ok": True, "owned_root": str(owned_root)}

    with (
        patch.object(usb_mount_broker, "_identity"),
        patch.object(usb_mount_broker, "_lease_for", return_value=None),
        patch.object(usb_mount_broker, "_claim", side_effect=claim),
    ):
        result = usb_mount_broker.handle(
            {
                "operation": "claim",
                "block_device": "/dev/fake1",
                "drive_device": "/dev/fake",
                "uuid": "FS-UUID-001",
                "serial": "USB-SERIAL-001",
                "consumer_id": "plankton-usb",
            },
            namespace_fd=4242,
        )

    assert result == {"ok": True, "owned_root": "/media/plankton-usb/FS-UUID-001"}
    assert observed == [(Path("/media/plankton-usb/FS-UUID-001"), 4242)]


def test_one_claim_remains_writable_through_flush_until_release(tmp_path: Path) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    device_id = agent.list_devices()[0].device_id
    assert agent.claim(device_id) == str(usb)
    agent.flush(device_id)

    assert [call["operation"] for call in host.broker_calls] == ["claim"]
    assert host.service_mount_mode == "rw"


def test_copy_reports_verified_image_progress(tmp_path: Path) -> None:
    agent, _, usb, jobs = _agent(tmp_path, copy_progress_images=2)
    source = usb / "science"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"jpeg-a")
    (source / "b.png").write_bytes(b"png-b")
    (source / "c.jpg").write_bytes(b"jpeg-c")
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)
    observed: list[tuple[int, int]] = []

    result = dispatch(
        agent,
        {
            "operation": "copy_to_staging",
            "args": {
                "device_id": device_id,
                "selected_relative_path": "science",
                "staging_directory": str(jobs / "staging"),
            },
        },
        lambda processed, total: observed.append((processed, total)),
    )

    assert result == {"entry_count": 3}
    assert observed == [(2, 3), (3, 3)]


@pytest.mark.parametrize(
    ("host_filesystem", "filesystem_version", "public_filesystem"),
    [
        ("vfat", "FAT16", "fat16"),
        ("vfat", "FAT32", "fat32"),
        ("exfat", "1.0", "exfat"),
        ("ntfs3", "1.0", "ntfs"),
        ("ext4", "1.0", "ext4"),
    ],
)
def test_preflight_progress_contract_covers_every_supported_filesystem(
    tmp_path: Path,
    host_filesystem: str,
    filesystem_version: str,
    public_filesystem: str,
) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    host.filesystem = host_filesystem
    host.filesystem_version = filesystem_version
    source = usb / "science"
    source.mkdir()
    for index in range(501):
        (source / f"image-{index:03}.jpg").write_bytes(b"x")
    ignored = source / "inference_results_001"
    ignored.mkdir()
    (ignored / "must-not-be-imported.jpg").write_bytes(b"private-result")
    device = agent.list_devices()[0]
    assert device.filesystem == public_filesystem
    agent.claim(device.device_id)
    observed: list[tuple[object, ...]] = []

    result = dispatch(
        agent,
        {
            "operation": "preflight_selected_tree",
            "args": {
                "device_id": device.device_id,
                "selected_relative_path": "science",
            },
        },
        lambda *values: observed.append(values),
    )

    assert isinstance(result, dict)
    assert result == {
        "file_count": 501,
        "source_bytes": 501,
        "required_free_bytes": int(501 * 2.2),
        "local_free_bytes": result["local_free_bytes"],
        "has_enough_space": True,
    }
    assert observed == [
        ("discovering", 500, None),
        ("discovering", 501, 501),
        ("measuring", 500, 501),
        ("measuring", 501, 501),
    ]


def test_threat_blocks_import_before_copy(tmp_path: Path) -> None:
    agent, host, usb, jobs = _agent(tmp_path)
    source = usb / "science"
    source.mkdir()
    threat = source / "eicar.jpg"
    threat.write_bytes(b"test")
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)
    host.scan_return_code = 1
    host.scan_stdout = f"{threat}: Eicar-Test-Signature FOUND\n"

    with pytest.raises(SecurityBlocked) as raised:
        agent.copy_to_staging(device_id, "science", jobs / "blocked")

    assert raised.value.relative_paths == ("science/eicar.jpg",)
    assert not (jobs / "blocked").exists()


def test_scan_streams_directory_contents_to_clamav(tmp_path: Path) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    (usb / "science").mkdir()
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)

    agent.scan_selected_tree(device_id, "science")

    assert (
        "clamdscan",
        "--stream",
        "--infected",
        "--no-summary",
        "--",
        str(usb / "science"),
    ) in host.calls


@pytest.mark.parametrize("replacement_serial", ["", "DIFFERENT-USB"])
def test_containment_and_same_device_identity(
    tmp_path: Path, replacement_serial: str
) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    (usb / "science").mkdir()
    (usb / "outside").mkdir()
    (usb / "science" / "escape").symlink_to(usb / "outside", target_is_directory=True)
    device_id = agent.list_devices()[0].device_id
    with pytest.raises(UsbAgentError, match="relative"):
        agent.list_directory(device_id, "../outside")
    with pytest.raises(UsbAgentError, match="symbolic"):
        agent.list_directory(device_id, "science/escape")

    host.serial = replacement_serial
    with pytest.raises(UsbAgentError, match="absent"):
        agent.list_directory(device_id, "science")


def test_socket_dispatch_has_fixed_operations(tmp_path: Path) -> None:
    agent, _, _, _ = _agent(tmp_path)
    result = dispatch(agent, {"operation": "list_devices"})
    assert isinstance(result, list)
    assert result[0]["uuid"] == "FS-UUID-001"
    assert dispatch(agent, {"operation": "has_unsupported_removable_media"}) is True
    with pytest.raises(UsbAgentError, match="unsupported"):
        dispatch(agent, {"operation": "run_command", "args": {"command": "id"}})


@pytest.mark.parametrize(("filesystem", "expected"), [("exfat", "exfat"), ("ntfs3", "ntfs")])
def test_supported_filesystem_aliases(tmp_path: Path, filesystem: str, expected: str) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.filesystem = filesystem
    assert agent.list_devices()[0].filesystem == expected


@pytest.mark.parametrize(
    ("filesystem_version", "expected"),
    [("FAT32", "fat32"), ("FAT16", "fat16"), ("FAT12", None)],
)
def test_supported_fat_versions_are_exposed_from_the_vfat_host_alias(
    tmp_path: Path, filesystem_version: str, expected: str | None
) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.filesystem = "vfat"
    host.filesystem_version = filesystem_version

    devices = agent.list_devices()

    assert [device.filesystem for device in devices] == ([] if expected is None else [expected])


@pytest.mark.parametrize(
    ("filesystem", "filesystem_version"),
    [("vfat", "FAT12"), ("vfat", "UNKNOWN"), ("btrfs", "1.0")],
)
def test_unsupported_removable_media_is_reported_without_device_identity(
    tmp_path: Path, filesystem: str, filesystem_version: str
) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.filesystem = filesystem
    host.filesystem_version = filesystem_version

    devices = dispatch(agent, {"operation": "list_devices"})
    availability = dispatch(agent, {"operation": "has_unsupported_removable_media"})

    assert devices == []
    assert availability is True
    public_payload = json.dumps([devices, availability])
    for private_value in (
        "/dev/fake",
        "USB-SERIAL-001",
        "FS-UUID-001",
        "PRIVATE-LABEL",
        filesystem,
        filesystem_version,
    ):
        assert private_value not in public_payload


def test_no_device_is_distinct_from_unsupported_media(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.lsblk_payload = {"blockdevices": []}

    assert dispatch(agent, {"operation": "list_devices"}) == []
    assert dispatch(agent, {"operation": "has_unsupported_removable_media"}) is False


def test_missing_filesystem_uuid_is_reported_without_device_identity(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.uuid = ""
    host.lsblk_payload = {
        "blockdevices": [
            {
                "path": "/dev/fake",
                "type": "disk",
                "rm": True,
                "tran": "usb",
                "serial": host.serial,
                "size": 4096,
                "children": [
                    {
                        "path": "/dev/fake1",
                        "type": "part",
                        "fstype": "ext4",
                        "fsver": "1.0",
                        "uuid": "",
                        "label": host.label,
                        "size": 4096,
                    }
                ],
            }
        ]
    }

    assert dispatch(agent, {"operation": "list_devices"}) == []
    availability = dispatch(agent, {"operation": "device_availability"})

    assert availability == {
        "unsupported_removable_media_detected": False,
        "missing_filesystem_uuid_detected": True,
    }
    public_payload = json.dumps(availability)
    for private_value in ("/dev/fake", "USB-SERIAL-001", "PRIVATE-LABEL"):
        assert private_value not in public_payload


def test_device_without_hardware_serial_uses_filesystem_uuid(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.serial = ""

    device = agent.list_devices()[0]

    assert device.uuid == "FS-UUID-001"
    assert device.serial == ""


def test_flat_lsblk_preserves_parent_usb_identity_and_eject_target(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.lsblk_payload = {
        "blockdevices": [
            {
                "path": "/dev/sdz",
                "type": "disk",
                "rm": True,
                "tran": "usb",
                "serial": "P5GATEH0001",
                "size": 4096,
            },
            {
                "path": "/dev/sdz1",
                "pkname": "sdz",
                "type": "part",
                "fstype": "ext4",
                "uuid": "11111111-2222-3333-4444-555555555555",
                "size": 4096,
            },
        ]
    }

    (device,) = agent.list_devices()

    assert device.block_device == "/dev/sdz1"
    assert device.drive_device == "/dev/sdz"
    assert device.serial == "P5GATEH0001"
    assert device.uuid == "11111111-2222-3333-4444-555555555555"
    agent.claim(device.device_id)
    agent.eject(device.device_id)
    assert (
        "udisksctl",
        "power-off",
        "--block-device",
        "/dev/sdz",
        "--no-user-interaction",
    ) in host.calls


def test_device_recorded_without_serial_accepts_same_uuid_when_serial_appears(
    tmp_path: Path,
) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    (usb / "science").mkdir()
    host.serial = ""
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)

    host.serial = "NOW-AVAILABLE"

    assert agent.preflight_selected_tree(device_id, "science").file_count == 0


def test_device_without_filesystem_uuid_is_not_selectable(tmp_path: Path) -> None:
    agent, host, _, _ = _agent(tmp_path)
    host.uuid = ""

    assert agent.list_devices() == ()


def test_missing_signatures_fail_closed(tmp_path: Path) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    (usb / "science").mkdir()
    for signature in agent.signature_directory.iterdir():
        signature.unlink()
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)

    with pytest.raises(ScannerUnavailable, match="unavailable"):
        agent.scan_selected_tree(device_id, "science")

    assert not any(call[0] == "clamdscan" for call in host.calls)


def test_signature_age_limit_allows_equal_and_rejects_one_second_over(
    tmp_path: Path,
) -> None:
    agent, host, usb, _ = _agent(tmp_path)
    (usb / "science").mkdir()
    device_id = agent.list_devices()[0].device_id
    agent.claim(device_id)
    signature = agent.signature_directory / "daily.cvd"
    now = datetime(2026, 8, 9, tzinfo=UTC)

    with patch("plankton_usb_agent.agent.datetime") as clock:
        clock.now.return_value = now
        equal_timestamp = now.timestamp() - agent.max_signature_age_hours * 3600
        os.utime(signature, (equal_timestamp, equal_timestamp))
        agent.scan_selected_tree(device_id, "science")

        over_timestamp = equal_timestamp - 1
        os.utime(signature, (over_timestamp, over_timestamp))
        with pytest.raises(ScannerUnavailable, match="stale"):
            agent.scan_selected_tree(device_id, "science")

    assert sum(call[0] == "clamdscan" for call in host.calls) == 1
