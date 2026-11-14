from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Literal

import pytest

from testing.usb_ownership_contract import (
    AccessProbe,
    ActionEvidence,
    AuthorityProbe,
    BrowseEvidence,
    ClaimEvidence,
    MountView,
    RetainedPreflightEvidence,
    StableStateEvidence,
    TransitionEvidence,
    evaluate_action_progress,
    evaluate_browse,
    evaluate_claim,
    evaluate_retained_preflight,
    evaluate_stable_state,
    evaluate_transition,
)


def _safe_browse() -> BrowseEvidence:
    source = "device:qa"
    return BrowseEvidence(
        expected_source=source,
        public_tree_statuses=(200, 200),
        identity_verified=True,
        native=MountView(source, "native:qa", frozenset({"rw", "nodev", "nosuid", "noexec"})),
        operator_access=AccessProbe.readwrite(),
        lease_absent=True,
        consumer=MountView.absent(),
        unrelated=AuthorityProbe.none(),
        application_container=AuthorityProbe.none(),
        public_output_sanitized=True,
    )


def _safe_claim() -> ClaimEvidence:
    source = "device:qa"
    return ClaimEvidence(
        expected_source=source,
        expected_owned_target="owned:qa",
        identity_verified=True,
        lease_current=True,
        mount_state_unambiguous=True,
        native=MountView.absent(),
        consumer=MountView(
            source,
            "owned:qa",
            frozenset({"rw", "nodev", "nosuid", "noexec"}),
        ),
        consumer_is_restricted=True,
        consumer_access=AccessProbe.readwrite(),
        operator_access=AccessProbe.unavailable(),
        unrelated=AuthorityProbe.none(),
        application_container=AuthorityProbe.none(),
        preflight_started_after_claim=True,
        public_output_sanitized=True,
        restricted_audit_complete=True,
        replacement_device_mutated=False,
    )


def _safe_retained_preflight() -> RetainedPreflightEvidence:
    return RetainedPreflightEvidence(
        claim=_safe_claim(),
        lifecycle_id_present=True,
        lifecycle_status="completed",
        durable_job_exists=False,
        desktop_mount_attempted=True,
        desktop_mount_blocked=True,
        ownership_message_visible=True,
    )


def _native_rw_recovery() -> StableStateEvidence:
    return StableStateEvidence(
        expected_state="NATIVE_RW",
        expected_source="device:qa",
        identity_verified=True,
        mount_state_unambiguous=True,
        lease_absent=True,
        native=MountView(
            "device:qa",
            "native:qa",
            frozenset({"rw", "nodev", "nosuid", "noexec"}),
        ),
        consumer=MountView.absent(),
        operator_access=AccessProbe.readwrite(),
        unrelated=AuthorityProbe.none(),
        application_container=AuthorityProbe.none(),
    )


def test_public_browse_is_native_rw_and_non_owning() -> None:
    result = evaluate_browse(_safe_browse())

    assert result.status == "PASS"
    assert result.violations == ()


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [
        (lambda value: replace(value, public_tree_statuses=(200,)), "public_tree_not_twice_successful"),
        (lambda value: replace(value, identity_verified=False), "device_identity_unverified"),
        (
            lambda value: replace(
                value,
                native=MountView(value.expected_source, "native:qa", frozenset({"ro"})),
            ),
            "browse_changed_native_rw",
        ),
        (lambda value: replace(value, operator_access=AccessProbe.unavailable()), "browse_operator_rw_missing"),
        (lambda value: replace(value, lease_absent=False), "browse_created_lease"),
        (
            lambda value: replace(
                value,
                consumer=MountView(value.expected_source, "owned:qa", frozenset({"rw"})),
            ),
            "browse_created_owned_view",
        ),
        (lambda value: replace(value, unrelated=AuthorityProbe(True, False)), "unrelated_authority_exposed"),
        (
            lambda value: replace(value, application_container=AuthorityProbe(False, True)),
            "container_authority_exposed",
        ),
        (lambda value: replace(value, public_output_sanitized=False), "public_output_not_sanitized"),
    ],
)
def test_browse_rejects_ownership_or_excess_authority(
    mutate: Callable[[BrowseEvidence], BrowseEvidence], violation: str
) -> None:
    result = evaluate_browse(mutate(_safe_browse()))

    assert result.status == "FAIL"
    assert violation in result.violations


def test_complete_exclusive_claim_is_owned() -> None:
    result = evaluate_claim(_safe_claim())

    assert result.status == "PASS"
    assert result.violations == ()


def test_retained_preflight_blocks_desktop_remount_and_explains_ownership() -> None:
    result = evaluate_retained_preflight(_safe_retained_preflight())

    assert result.status == "PASS"
    assert result.violations == ()
    assert result.advisories == ()


def test_retained_preflight_rejects_a_desktop_dual_rw_remount() -> None:
    unsafe_claim = replace(
        _safe_claim(),
        native=MountView("device:qa", "native:qa", frozenset({"rw"})),
        operator_access=AccessProbe.readwrite(),
    )

    result = evaluate_retained_preflight(
        replace(
            _safe_retained_preflight(),
            claim=unsafe_claim,
            desktop_mount_blocked=False,
        )
    )

    assert result.status == "FAIL"
    assert "native_view_residue" in result.violations
    assert "operator_owned_view_exposed" in result.violations
    assert "desktop_remount_not_blocked" in result.violations


def test_missing_ownership_explanation_is_a_ux_advisory() -> None:
    result = evaluate_retained_preflight(
        replace(_safe_retained_preflight(), ownership_message_visible=False)
    )

    assert result.status == "PASS"
    assert result.violations == ()
    assert result.advisories == ("ownership_explanation_missing",)


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [
        (
            lambda value: replace(value, lifecycle_id_present=False),
            "preflight_lifecycle_id_missing",
        ),
        (
            lambda value: replace(value, lifecycle_status="failed"),
            "preflight_lifecycle_not_retained",
        ),
        (
            lambda value: replace(value, durable_job_exists=True),
            "durable_job_already_exists",
        ),
        (
            lambda value: replace(value, desktop_mount_attempted=False),
            "desktop_remount_probe_missing",
        ),
    ],
)
def test_retained_preflight_rejects_incomplete_evidence(
    mutate: Callable[[RetainedPreflightEvidence], RetainedPreflightEvidence],
    violation: str,
) -> None:
    result = evaluate_retained_preflight(mutate(_safe_retained_preflight()))

    assert result.status == "FAIL"
    assert violation in result.violations


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [
        (lambda value: replace(value, identity_verified=False), "device_identity_unverified"),
        (lambda value: replace(value, lease_current=False), "ownership_lease_stale"),
        (lambda value: replace(value, mount_state_unambiguous=False), "mount_state_ambiguous"),
        (
            lambda value: replace(
                value,
                native=MountView(value.expected_source, "native:qa", frozenset({"ro"})),
            ),
            "native_view_residue",
        ),
        (lambda value: replace(value, consumer=MountView.absent()), "consumer_source_mismatch"),
        (
            lambda value: replace(
                value,
                consumer=MountView(value.expected_source, "owned:wrong", frozenset({"rw"})),
            ),
            "consumer_not_hardened_rw",
        ),
        (lambda value: replace(value, consumer_is_restricted=False), "consumer_identity_unrestricted"),
        (lambda value: replace(value, consumer_access=AccessProbe.unavailable()), "consumer_access_unproven"),
        (lambda value: replace(value, operator_access=AccessProbe.readwrite()), "operator_owned_view_exposed"),
        (lambda value: replace(value, unrelated=AuthorityProbe(True, False)), "unrelated_authority_exposed"),
        (
            lambda value: replace(value, application_container=AuthorityProbe(False, True)),
            "container_authority_exposed",
        ),
        (
            lambda value: replace(value, preflight_started_after_claim=False),
            "preflight_started_before_verified_claim",
        ),
        (lambda value: replace(value, public_output_sanitized=False), "public_output_not_sanitized"),
        (lambda value: replace(value, restricted_audit_complete=False), "restricted_audit_incomplete"),
        (lambda value: replace(value, replacement_device_mutated=True), "replacement_device_modified"),
    ],
)
def test_claim_rejects_incomplete_or_excess_authority(
    mutate: Callable[[ClaimEvidence], ClaimEvidence], violation: str
) -> None:
    result = evaluate_claim(mutate(_safe_claim()))

    assert result.status == "FAIL"
    assert violation in result.violations


@pytest.mark.parametrize(
    "options",
    [
        frozenset(),
        frozenset({"rw"}),
        frozenset({"ro", "nodev", "nosuid", "noexec"}),
        frozenset({"ro", "rw", "nodev", "nosuid", "noexec"}),
    ],
)
def test_claim_rejects_each_consumer_mount_option_boundary(options: frozenset[str]) -> None:
    evidence = _safe_claim()
    result = evaluate_claim(
        replace(
            evidence,
            consumer=MountView(evidence.expected_source, evidence.expected_owned_target, options),
        )
    )

    assert result.status == "FAIL"
    assert "consumer_not_hardened_rw" in result.violations


def test_ordinary_failure_recovers_native_rw_without_owned_view_or_lease() -> None:
    result = evaluate_stable_state(_native_rw_recovery())

    assert result.status == "PASS"
    assert result.violations == ()


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [
        (lambda value: replace(value, identity_verified=False), "device_identity_unverified"),
        (lambda value: replace(value, mount_state_unambiguous=False), "mount_state_ambiguous"),
        (lambda value: replace(value, lease_absent=False), "ownership_lease_residue"),
        (
            lambda value: replace(
                value,
                consumer=MountView(value.expected_source, "owned:qa", frozenset({"rw"})),
            ),
            "owned_view_residue",
        ),
        (
            lambda value: replace(
                value,
                native=MountView(value.expected_source, "native:qa", frozenset({"ro"})),
            ),
            "native_not_readwrite",
        ),
        (lambda value: replace(value, operator_access=AccessProbe.unavailable()), "operator_access_not_restored"),
        (lambda value: replace(value, unrelated=AuthorityProbe(True, False)), "unrelated_authority_exposed"),
        (
            lambda value: replace(value, application_container=AuthorityProbe(False, True)),
            "container_authority_exposed",
        ),
    ],
)
def test_recovery_rejects_residue_or_excess_authority(
    mutate: Callable[[StableStateEvidence], StableStateEvidence], violation: str
) -> None:
    result = evaluate_stable_state(mutate(_native_rw_recovery()))

    assert result.status == "FAIL"
    assert violation in result.violations


def test_security_block_reaches_ejected_state() -> None:
    result = evaluate_stable_state(
        replace(
            _native_rw_recovery(),
            expected_state="EJECTED",
            native=MountView.absent(),
            operator_access=AccessProbe.unavailable(),
        )
    )

    assert result.status == "PASS"
    assert result.violations == ()


def test_security_block_must_not_hand_back_native_rw() -> None:
    result = evaluate_stable_state(replace(_native_rw_recovery(), expected_state="EJECTED"))

    assert result.status == "FAIL"
    assert "device_not_ejected" in result.violations


@pytest.mark.parametrize("result", ["zero", "nonzero"])
def test_action_may_advance_only_after_its_exact_postcondition(
    result: Literal["zero", "nonzero"],
) -> None:
    evaluated = evaluate_action_progress(
        ActionEvidence(
            result=result,
            postcondition="complete_exact",
            restricted_audit_complete=True,
        )
    )

    assert evaluated.status == "PASS"


@pytest.mark.parametrize(
    "postcondition",
    ["incomplete", "foreign", "ambiguous", "unreadable"],
)
@pytest.mark.parametrize("result", ["zero", "nonzero"])
def test_action_cannot_advance_when_its_postcondition_is_unproven(
    result: Literal["zero", "nonzero"],
    postcondition: Literal["incomplete", "foreign", "ambiguous", "unreadable"],
) -> None:
    evaluated = evaluate_action_progress(
        ActionEvidence(
            result=result,
            postcondition=postcondition,
            restricted_audit_complete=True,
        )
    )

    assert evaluated.status == "FAIL"
    assert "action_postcondition_unproven" in evaluated.violations


def test_timeout_never_advances_even_if_a_stale_observation_looks_complete() -> None:
    evaluated = evaluate_action_progress(
        ActionEvidence("timeout", "complete_exact", restricted_audit_complete=True)
    )

    assert evaluated.status == "FAIL"
    assert evaluated.violations == ("action_postcondition_unproven",)


def test_nonzero_action_requires_restricted_diagnostic_audit() -> None:
    evaluated = evaluate_action_progress(
        ActionEvidence("nonzero", "complete_exact", restricted_audit_complete=False)
    )

    assert evaluated.status == "FAIL"
    assert evaluated.violations == ("action_diagnostic_not_audited",)


def _committed_transition() -> TransitionEvidence:
    return TransitionEvidence(
        target_verified=True,
        commit_record_durable=True,
        reconciliation_retained=False,
        public_status=200,
        stale_snapshot_returned=False,
        caller_selected_authority=False,
        replacement_device_mutated=False,
    )


def _pending_transition() -> TransitionEvidence:
    return TransitionEvidence(
        target_verified=False,
        commit_record_durable=False,
        reconciliation_retained=True,
        public_status=503,
        stale_snapshot_returned=False,
        caller_selected_authority=False,
        replacement_device_mutated=False,
    )


def test_verified_durable_transition_may_report_public_success() -> None:
    assert evaluate_transition(_committed_transition()).status == "PASS"


def test_incomplete_transition_is_safe_only_when_retryable_and_non_successful() -> None:
    assert evaluate_transition(_pending_transition()).status == "PASS"


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [
        (
            lambda value: replace(value, public_status=200),
            "incomplete_transition_reported_success",
        ),
        (
            lambda value: replace(value, reconciliation_retained=False),
            "incomplete_transition_lost_reconciliation",
        ),
        (
            lambda value: replace(value, commit_record_durable=True),
            "transition_finalized_before_target",
        ),
        (
            lambda value: replace(value, stale_snapshot_returned=True),
            "stale_snapshot_returned",
        ),
        (
            lambda value: replace(value, caller_selected_authority=True),
            "caller_selected_authority",
        ),
        (
            lambda value: replace(value, replacement_device_mutated=True),
            "replacement_device_modified",
        ),
    ],
)
def test_pending_transition_rejects_false_commit_or_authority_expansion(
    mutate: Callable[[TransitionEvidence], TransitionEvidence], violation: str
) -> None:
    evaluated = evaluate_transition(mutate(_pending_transition()))

    assert evaluated.status == "FAIL"
    assert violation in evaluated.violations


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [
        (
            lambda value: replace(value, public_status=503),
            "committed_transition_not_public_success",
        ),
        (
            lambda value: replace(value, reconciliation_retained=True),
            "committed_transition_retained_reconciliation",
        ),
    ],
)
def test_committed_transition_rejects_false_public_or_reconciliation_state(
    mutate: Callable[[TransitionEvidence], TransitionEvidence], violation: str
) -> None:
    evaluated = evaluate_transition(mutate(_committed_transition()))

    assert evaluated.status == "FAIL"
    assert violation in evaluated.violations
