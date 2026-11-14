"""Issue #296: interrupted recovery must not strand a block-path Polkit guard.

Run against merged PR #298 or later. Reuse its fake OS, but exercise real broker
transactions, durable lease persistence and guard files. No physical device I/O.
"""

from typing import Any

import pytest

from host import usb_mount_broker as broker

pytest_plugins = ("tests.unit.test_issue296_broker_recovery",)


@pytest.mark.parametrize("changes_path_again", [False, True])
def test_retry_after_second_return_removes_obsolete_guard(
    recovery: Any, changes_path_again: bool
) -> None:
    # First return is temporarily busy: retain responsibility, not success.
    recovery.busy = True
    with pytest.raises(broker.BrokerError, match="recovery is pending"):
        broker.handle(recovery.request, namespace_fd=42)
    previous_return = recovery.request["block_device"]
    assert broker._guard_marker(previous_return).exists()

    # Keep a genuinely unrelated lease/guard as a negative cleanup control.
    unrelated = {
        **recovery.lease,
        "block_device": "/dev/unrelated1",
        "uuid": "unrelated",
        "serial": "unrelated",
        "lease_id": "unrelated",
    }
    leases = broker._load_leases()
    leases[broker._lease_key(unrelated)] = unrelated
    broker._save_leases(leases)
    broker._guard_acquire(unrelated["block_device"])

    if changes_path_again:
        # The same original USB disconnects/returns before the pending retry.
        recovery.seq = "12"
        recovery.request.update(
            block_device="/dev/returned_again1", drive_device="/dev/returned_again"
        )
    recovery.busy = False
    response = broker.handle(recovery.request, namespace_fd=42)
    assert response["ok"] and response["lease_id"] == "original"
    persisted = broker._load_leases()
    assert persisted[broker._lease_key(unrelated)] == unrelated
    assert len(persisted) == 2
    current = persisted[broker._lease_key(recovery.request)]
    assert "recovering" not in current and "recovery_block" not in current

    # Public success cannot leave an orphan denying access to a reused sdX path.
    assert {p.read_text() for p in broker.MOUNT_GUARD_DIR.iterdir()} == {
        recovery.request["block_device"], unrelated["block_device"]
    }
