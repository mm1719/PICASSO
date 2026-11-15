"""Public agent cleanup after an unacknowledged claim; only host/transport are fake."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from plankton_api import ApiSettings, create_app
from plankton_control import UsbClientError
from plankton_core import JobStore
from plankton_usb_agent import UsbAgent, UsbAgentError
from plankton_usb_agent.server import dispatch

from host import usb_mount_broker as broker

pytest_plugins = ("tests.unit.test_issue322_broker_cleanup",)


def _settings(tmp_path: Path) -> ApiSettings:
    # Keep the temporary API fixture independent of the unexported integration suite.
    return ApiSettings(
        database_path=tmp_path / "app.db",
        job_root=tmp_path / "jobs",
        usb_socket_path=tmp_path / "usb.sock",
        gpu_lock_path=tmp_path / "run" / "gpu.lock",
        checkpoint_sha256="a" * 64,
    )


@pytest.fixture
def agent_host(claim_host: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    state = claim_host
    state.operations = []
    state.lose_reply = False
    state.database = tmp_path / "app.db"
    state.store = JobStore(state.database)
    state.store.initialize()
    monkeypatch.setattr(broker, "_job_database", lambda _fd: state.database)

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] == "lsblk":
            result = {"blockdevices": [{
                "path": state.request["drive_device"], "type": "disk", "rm": True,
                "tran": "usb", "serial": state.request["serial"], "size": 4096,
                "children": [{
                    "path": state.request["block_device"], "type": "part", "fstype": "ext4",
                    "uuid": state.request["uuid"], "size": 4096,
                }],
            }]}
        elif argv[0] == "findmnt":
            result = {"filesystems": [
                {"target": str(path), "options": ",".join(sorted(options))}
                for path, options in state.consumer.items()
            ]}
        elif argv[0] == "/usr/bin/gdbus":
            assert state.native and not state.consumer
            return subprocess.CompletedProcess(argv, 0, f"('{state.native_path}',)", "")
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    def request(payload: dict[str, str]) -> dict[str, object]:
        state.operations.append(payload["operation"])
        try:
            result = broker.handle(payload, namespace_fd=4242)
        except broker.BrokerError as error:
            result = {"ok": False, "error": str(error)}
        if state.lose_reply and payload["operation"] == "claim":
            state.lose_reply = False
            raise UsbAgentError("USB mount broker is unavailable")
        return result

    state.agent = UsbAgent(
        job_root=tmp_path / "jobs", run_command=run, mount_broker_request=request,
        ownership_root_parent=None,
    )
    state.device_id = state.agent.list_devices()[0].device_id

    class UsbTransport:
        def request(
            self, operation: str, *, progress: Callable[..., None] | None = None, **args: object,
        ) -> object:
            try:
                return dispatch(state.agent, {"operation": operation, "args": args}, progress)
            except UsbAgentError as error:
                raise UsbClientError(str(error)) from error

    state.usb = UsbTransport()
    return state


@pytest.mark.parametrize("fault", ["lost-success-reply", "failed-rollback"])
def test_public_release_recovers_unacknowledged_claim_without_agent_restart(
    agent_host: Any, fault: str,
) -> None:
    state = agent_host
    if fault == "lost-success-reply":
        state.lose_reply = True
    else:
        state.unmount_error = broker.BrokerError("detach timed out")
        state.restore_error = broker.BrokerError("native restore failed")
    args = {"device_id": state.device_id}
    with pytest.raises(UsbAgentError):
        dispatch(state.agent, {"operation": "claim", "args": args})
    assert not state.agent._ownership_leases
    assert broker._lease_for(state.request) is not None

    state.unmount_error = state.restore_error = None
    dispatch(state.agent, {"operation": "release", "args": args})

    assert broker._load_leases() == {}, "public release silently left durable claim responsibility"
    assert state.native and not state.consumer
    assert not broker._guard_marker(state.request["block_device"]).exists()
    assert dispatch(state.agent, {"operation": "claim", "args": args}) == str(state.owned)
    assert state.consumer and not state.native


def test_public_cleanup_failure_is_not_silent_success_and_can_be_retried(agent_host: Any) -> None:
    state = agent_host
    state.lose_reply = True
    args = {"device_id": state.device_id}
    with pytest.raises(UsbAgentError):
        dispatch(state.agent, {"operation": "claim", "args": args})
    before = broker._load_leases()
    state.restore_error = broker.BrokerError("native USB view was not restored")
    with pytest.raises(UsbAgentError, match="native USB view was not restored"):
        dispatch(state.agent, {"operation": "release", "args": args})
    assert broker._load_leases() == before and not state.native
    assert broker._guard_marker(state.request["block_device"]).exists()
    state.restore_error = None
    dispatch(state.agent, {"operation": "release", "args": args})
    assert broker._load_leases() == {} and state.native and not state.consumer


def test_public_release_without_local_lease_cannot_steal_a_nonterminal_job(agent_host: Any) -> None:
    state = agent_host
    state.lose_reply = True
    args = {"device_id": state.device_id}
    with pytest.raises(UsbAgentError):
        dispatch(state.agent, {"operation": "claim", "args": args})
    job = str(uuid4())
    state.store.create_requested_job(
        job, "model", "a" * 64, state.device_id, "science",
        device_uuid=state.request["uuid"], device_serial=state.request["serial"],
    )
    lease = broker._lease_for(state.request)
    assert lease is not None
    # Model an already durable job owner after the agent lost its local cache.
    lease.update(job_id=job, issued_at="2000-01-01T00:00:00+00:00")
    broker._save_leases({broker._lease_key(lease): lease})
    before = list(state.events)
    with pytest.raises(UsbAgentError, match="active job"):
        dispatch(state.agent, {"operation": "release", "args": args})
    assert state.events == before and state.consumer and not state.native
    assert broker._lease_for(state.request) == lease
    state.agent.reconcile_persisted_ownership()
    assert state.events == before and broker._lease_for(state.request) == lease


def test_public_release_without_any_lease_is_idempotent(agent_host: Any) -> None:
    args = {"device_id": agent_host.device_id}
    for _ in range(2):
        assert dispatch(agent_host.agent, {"operation": "release", "args": args}) is None
    assert agent_host.events == [] and agent_host.native and not agent_host.consumer
    assert broker._load_leases() == {}


@pytest.mark.parametrize("fault", ["lost-reply", "failed-rollback", "lost-reply-failed-cleanup"])
@pytest.mark.parametrize("resume", ["retry", "refresh"])
def test_http_preflight_recovers_failed_claim_without_service_restart(
    agent_host: Any, tmp_path: Path, fault: str, resume: str,
) -> None:
    state = agent_host
    state.lose_reply = fault != "failed-rollback"
    if fault == "failed-rollback":
        state.unmount_error = broker.BrokerError("detach timed out")
    if fault != "lost-reply":
        state.restore_error = broker.BrokerError("native USB view was not restored")
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, state.usb)) as client:
        assert client.post("/api/v1/session/open", json={"session_id": "window"}).status_code == 200
        url = f"/api/v1/devices/{state.device_id}/preflight"
        selection = {"session_id": "window"}
        failed = client.post(url, json=selection)
        assert failed.status_code == 503
        identifier = failed.headers["X-PICASSO-Preflight-ID"]
        snapshot = client.get(f"/api/v1/preflights/{identifier}").json()
        assert snapshot["status"] == "failed" and snapshot["summary"] is None
        assert snapshot["eta_seconds"] is None
        if fault != "lost-reply":
            pending = broker._load_leases()
            assert pending
            refused = (
                client.post(url, json=selection) if resume == "retry"
                else client.delete(f"/api/v1/preflights/{identifier}")
            )
            assert refused.status_code == 503 and broker._load_leases() == pending
            if resume == "retry":
                assert refused.headers["X-PICASSO-Preflight-ID"] == identifier
        state.unmount_error = state.restore_error = None
        if resume == "refresh":
            released = client.delete(f"/api/v1/preflights/{identifier}")
            assert released.status_code == 200
            assert released.json()["status"] == ("failed" if fault == "lost-reply" else "cancelled")
            assert broker._load_leases() == {} and state.native and not state.consumer
        retry = client.post(url, json=selection)
        assert retry.status_code in {200, 202}, retry.json()
        assert retry.json()["status"] in {"running", "completed"}
        assert client.delete(f"/api/v1/preflights/{retry.json()['preflight_id']}").status_code == 200
        assert broker._load_leases() == {} and state.native and not state.consumer


@pytest.mark.parametrize("session", [None, "window"])
def test_http_second_preflight_cannot_release_a_successful_first_owner(
    agent_host: Any, tmp_path: Path, session: str | None,
) -> None:
    state = agent_host
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, state.usb)) as client:
        if session:
            assert client.post("/api/v1/session/open", json={"session_id": session}).status_code == 200
        url = f"/api/v1/devices/{state.device_id}/preflight"
        selection = {} if session is None else {"session_id": session}
        first = client.post(url, json=selection)
        assert first.status_code in {200, 202}
        before = broker._load_leases()
        operations = list(state.operations)
        assert client.post(url, json=selection).status_code == 409
        assert broker._load_leases() == before and state.operations == operations
        assert state.consumer and not state.native
        assert client.delete(f"/api/v1/preflights/{first.json()['preflight_id']}").status_code == 200
