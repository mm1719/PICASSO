"""#328: a reviewed Preflight is not an accepted export job. No host disks or GPU."""

import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from plankton_api import ApiSettings, create_app
from plankton_control import UnixUsbClient
from plankton_usb_agent.server import UsbAgentServer
from plankton_worker import Worker, WorkerSettings

from tests.unit.test_stage3_usb_agent import _agent


# Inline only the synthetic fixtures needed from the unexported control suite.
class FakeBatchModel:
    calls = 0

    def predict_paths(self, relative_paths: Sequence[str]) -> dict[str, tuple[float, ...]]:
        type(self).calls += 1
        results: dict[str, tuple[float, ...]] = {}
        for index, path in enumerate(relative_paths):
            scores = [0.01] * 20
            scores[index] = 0.81
            results[path] = tuple(scores)
        return results


def _settings(tmp_path: Path) -> tuple[ApiSettings, WorkerSettings]:
    job_root = tmp_path / "jobs"
    database_path = tmp_path / "app.db"
    gpu_lock_path = tmp_path / "run" / "gpu.lock"
    return (
        ApiSettings(
            database_path=database_path,
            job_root=job_root,
            usb_socket_path=tmp_path / "usb.sock",
            gpu_lock_path=gpu_lock_path,
            checkpoint_sha256="a" * 64,
        ),
        WorkerSettings(database_path, job_root, gpu_lock_path, checkpoint_images=1),
    )


@pytest.fixture
def media(tmp_path: Path) -> Iterator[tuple[Path, Path, Path]]:
    agent, _, usb, jobs = _agent(tmp_path)
    # The caller's pytest --basetemp can exceed AF_UNIX's socket path limit.
    with TemporaryDirectory(prefix="p5-export-", dir="/tmp") as sockets:
        sock = Path(sockets) / "usb.sock"
        with UsbAgentServer(sock, agent) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield sock, usb, jobs
            finally:
                server.shutdown()
                thread.join(timeout=2)
                assert not thread.is_alive()


def reviewed_preflight(client: TestClient, device: str, window: str) -> str:
    response = client.post(
        f"/api/v1/devices/{device}/preflight",
        json={"session_id": window, "source_relative_path": "science"},
    )
    assert response.status_code in (200, 202), response.text
    run = response.json()
    deadline = time.monotonic() + 5
    while run["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        response = client.get(f"/api/v1/preflights/{run['preflight_id']}")
        assert response.status_code == 200, response.text
        run = response.json()
    assert run["status"] == "completed" and run["summary"]["file_count"] == 1, run
    return str(run["preflight_id"])


@pytest.mark.parametrize("leave", ["cancel", "close"])
def test_only_a_confirmed_job_publishes_results(
    tmp_path: Path, media: tuple[Path, Path, Path], leave: str,
) -> None:
    sock, usb, jobs = media
    settings, worker_settings = _settings(tmp_path)
    science = usb / "science"
    previous = science / "inference_results_001"
    previous.mkdir(parents=True)
    reference = previous / "notes.txt"
    reference.write_bytes(b"prior result; preserve")
    image = science / "sample.png"
    Image.new("RGB", (2, 2), "green").save(image)
    original = image.read_bytes()
    worker = Worker(worker_settings, UnixUsbClient(sock, timeout_seconds=5), lambda _: FakeBatchModel())

    def no_publication() -> None:
        assert not worker.run_once()
        assert not any(jobs.glob("*/outputs"))
        assert set(science.iterdir()) == {image, previous}
        assert set(previous.iterdir()) == {reference}
        assert image.read_bytes() == original and reference.read_bytes() == b"prior result; preserve"

    with TestClient(create_app(replace(settings, usb_socket_path=sock))) as client:
        devices = client.get("/api/v1/devices")
        assert devices.status_code == 200 and len(devices.json()) == 1
        device = devices.json()[0]["device_id"]
        assert client.post("/api/v1/session/open", json={"session_id": "one"}).json()["job"] is None
        reviewed = reviewed_preflight(client, device, "one")
        no_publication()
        if leave == "cancel":
            cancelled = client.delete(f"/api/v1/preflights/{reviewed}")
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        else:
            assert client.post("/api/v1/session/close", json={"session_id": "one"}).status_code == 200
        assert client.post("/api/v1/session/open", json={"session_id": "two"}).json()["job"] is None
        no_publication()

        confirmed = client.post("/api/v1/jobs", json={
            "session_id": "two", "device_id": device, "source_relative_path": "science",
            "preflight_id": reviewed_preflight(client, device, "two"),
        })
        assert confirmed.status_code == 202, confirmed.text
        job = confirmed.json()["job_id"]
        for _ in range(12):
            if not worker.run_once():
                break
        final = client.get(f"/api/v1/jobs/{job}")
        assert final.status_code == 200 and final.json()["state"] == "COMPLETED", final.text
        assert not worker.run_once()
    published = science / "inference_results_002"
    assert set(science.iterdir()) == {image, previous, published}
    assert set(previous.iterdir()) == {reference}
    assert reference.read_bytes() == b"prior result; preserve" and image.read_bytes() == original
    local = jobs / job / "outputs"
    assert json.loads((published / "job_summary.json").read_bytes())["job_id"] == job
    assert {p.name: p.read_bytes() for p in published.iterdir()} == {
        p.name: p.read_bytes() for p in local.iterdir()
    }
