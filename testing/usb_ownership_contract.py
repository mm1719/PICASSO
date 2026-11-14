from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["PASS", "FAIL"]
StableState = Literal["NATIVE_RW", "EJECTED"]
ActionResult = Literal["zero", "nonzero", "timeout"]
ActionPostcondition = Literal[
    "complete_exact", "incomplete", "foreign", "ambiguous", "unreadable"
]
RetainedLifecycleStatus = Literal["running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class MountView:
    source: str | None
    target: str | None
    options: frozenset[str]

    @classmethod
    def absent(cls) -> MountView:
        return cls(None, None, frozenset())


@dataclass(frozen=True, slots=True)
class AccessProbe:
    read: bool
    create: bool
    rename: bool
    delete: bool
    write: bool

    @classmethod
    def readwrite(cls) -> AccessProbe:
        return cls(True, True, True, True, True)

    @classmethod
    def unavailable(cls) -> AccessProbe:
        return cls(False, False, False, False, False)


@dataclass(frozen=True, slots=True)
class AuthorityProbe:
    owned_write: bool
    raw_device: bool

    @classmethod
    def none(cls) -> AuthorityProbe:
        return cls(False, False)


@dataclass(frozen=True, slots=True)
class BrowseEvidence:
    expected_source: str
    public_tree_statuses: tuple[int, ...]
    identity_verified: bool
    native: MountView
    operator_access: AccessProbe
    lease_absent: bool
    consumer: MountView
    unrelated: AuthorityProbe
    application_container: AuthorityProbe
    public_output_sanitized: bool


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    expected_source: str
    expected_owned_target: str
    identity_verified: bool
    lease_current: bool
    mount_state_unambiguous: bool
    native: MountView
    consumer: MountView
    consumer_is_restricted: bool
    consumer_access: AccessProbe
    operator_access: AccessProbe
    unrelated: AuthorityProbe
    application_container: AuthorityProbe
    preflight_started_after_claim: bool
    public_output_sanitized: bool
    restricted_audit_complete: bool
    replacement_device_mutated: bool


@dataclass(frozen=True, slots=True)
class RetainedPreflightEvidence:
    claim: ClaimEvidence
    lifecycle_id_present: bool
    lifecycle_status: RetainedLifecycleStatus
    durable_job_exists: bool
    desktop_mount_attempted: bool
    desktop_mount_blocked: bool
    ownership_message_visible: bool


@dataclass(frozen=True, slots=True)
class StableStateEvidence:
    expected_state: StableState
    expected_source: str
    identity_verified: bool
    mount_state_unambiguous: bool
    lease_absent: bool
    native: MountView
    consumer: MountView
    operator_access: AccessProbe
    unrelated: AuthorityProbe
    application_container: AuthorityProbe


@dataclass(frozen=True, slots=True)
class ActionEvidence:
    result: ActionResult
    postcondition: ActionPostcondition
    restricted_audit_complete: bool


@dataclass(frozen=True, slots=True)
class TransitionEvidence:
    target_verified: bool
    commit_record_durable: bool
    reconciliation_retained: bool
    public_status: int
    stale_snapshot_returned: bool
    caller_selected_authority: bool
    replacement_device_mutated: bool


@dataclass(frozen=True, slots=True)
class ContractResult:
    status: Status
    violations: tuple[str, ...]
    advisories: tuple[str, ...] = ()


def _result(
    violations: list[str], advisories: list[str] | None = None
) -> ContractResult:
    return ContractResult(
        "PASS" if not violations else "FAIL",
        tuple(violations),
        tuple(advisories or ()),
    )


def evaluate_browse(evidence: BrowseEvidence) -> ContractResult:
    violations: list[str] = []
    if len(evidence.public_tree_statuses) < 2 or any(
        status != 200 for status in evidence.public_tree_statuses
    ):
        violations.append("public_tree_not_twice_successful")
    if not evidence.identity_verified:
        violations.append("device_identity_unverified")
    if (
        evidence.native.source != evidence.expected_source
        or "rw" not in evidence.native.options
        or "ro" in evidence.native.options
    ):
        violations.append("browse_changed_native_rw")
    if evidence.operator_access != AccessProbe.readwrite():
        violations.append("browse_operator_rw_missing")
    if not evidence.lease_absent:
        violations.append("browse_created_lease")
    if evidence.consumer != MountView.absent():
        violations.append("browse_created_owned_view")
    if evidence.unrelated != AuthorityProbe.none():
        violations.append("unrelated_authority_exposed")
    if evidence.application_container != AuthorityProbe.none():
        violations.append("container_authority_exposed")
    if not evidence.public_output_sanitized:
        violations.append("public_output_not_sanitized")
    return _result(violations)


def evaluate_claim(evidence: ClaimEvidence) -> ContractResult:
    violations: list[str] = []
    if not evidence.identity_verified:
        violations.append("device_identity_unverified")
    if not evidence.lease_current:
        violations.append("ownership_lease_stale")
    if not evidence.mount_state_unambiguous:
        violations.append("mount_state_ambiguous")
    if evidence.native != MountView.absent():
        violations.append("native_view_residue")
    if evidence.consumer.source != evidence.expected_source:
        violations.append("consumer_source_mismatch")
    required = {"rw", "nodev", "nosuid", "noexec"}
    if (
        evidence.consumer.target != evidence.expected_owned_target
        or not required.issubset(evidence.consumer.options)
        or "ro" in evidence.consumer.options
    ):
        violations.append("consumer_not_hardened_rw")
    if not evidence.consumer_is_restricted:
        violations.append("consumer_identity_unrestricted")
    if evidence.consumer_access != AccessProbe.readwrite():
        violations.append("consumer_access_unproven")
    if evidence.operator_access != AccessProbe.unavailable():
        violations.append("operator_owned_view_exposed")
    if evidence.unrelated != AuthorityProbe.none():
        violations.append("unrelated_authority_exposed")
    if evidence.application_container != AuthorityProbe.none():
        violations.append("container_authority_exposed")
    if not evidence.preflight_started_after_claim:
        violations.append("preflight_started_before_verified_claim")
    if not evidence.public_output_sanitized:
        violations.append("public_output_not_sanitized")
    if not evidence.restricted_audit_complete:
        violations.append("restricted_audit_incomplete")
    if evidence.replacement_device_mutated:
        violations.append("replacement_device_modified")
    return _result(violations)


def evaluate_retained_preflight(evidence: RetainedPreflightEvidence) -> ContractResult:
    violations = list(evaluate_claim(evidence.claim).violations)
    advisories: list[str] = []
    if not evidence.lifecycle_id_present:
        violations.append("preflight_lifecycle_id_missing")
    if evidence.lifecycle_status not in {"running", "completed"}:
        violations.append("preflight_lifecycle_not_retained")
    if evidence.durable_job_exists:
        violations.append("durable_job_already_exists")
    if not evidence.desktop_mount_attempted:
        violations.append("desktop_remount_probe_missing")
    if not evidence.desktop_mount_blocked:
        violations.append("desktop_remount_not_blocked")
    if not evidence.ownership_message_visible:
        advisories.append("ownership_explanation_missing")
    return _result(violations, advisories)


def evaluate_stable_state(evidence: StableStateEvidence) -> ContractResult:
    violations: list[str] = []
    if not evidence.identity_verified:
        violations.append("device_identity_unverified")
    if not evidence.mount_state_unambiguous:
        violations.append("mount_state_ambiguous")
    if not evidence.lease_absent:
        violations.append("ownership_lease_residue")
    if evidence.consumer != MountView.absent():
        violations.append("owned_view_residue")
    if evidence.expected_state == "NATIVE_RW":
        if (
            evidence.native.source != evidence.expected_source
            or "rw" not in evidence.native.options
            or "ro" in evidence.native.options
        ):
            violations.append("native_not_readwrite")
        if evidence.operator_access != AccessProbe.readwrite():
            violations.append("operator_access_not_restored")
    elif evidence.native != MountView.absent() or evidence.operator_access != AccessProbe.unavailable():
        violations.append("device_not_ejected")
    if evidence.unrelated != AuthorityProbe.none():
        violations.append("unrelated_authority_exposed")
    if evidence.application_container != AuthorityProbe.none():
        violations.append("container_authority_exposed")
    return _result(violations)


def evaluate_action_progress(evidence: ActionEvidence) -> ContractResult:
    violations: list[str] = []
    if evidence.result == "timeout" or evidence.postcondition != "complete_exact":
        violations.append("action_postcondition_unproven")
    if evidence.result == "nonzero" and not evidence.restricted_audit_complete:
        violations.append("action_diagnostic_not_audited")
    return _result(violations)


def evaluate_transition(evidence: TransitionEvidence) -> ContractResult:
    violations: list[str] = []
    committed = evidence.target_verified and evidence.commit_record_durable
    if evidence.caller_selected_authority:
        violations.append("caller_selected_authority")
    if evidence.replacement_device_mutated:
        violations.append("replacement_device_modified")
    if evidence.stale_snapshot_returned:
        violations.append("stale_snapshot_returned")
    if committed:
        if not 200 <= evidence.public_status < 300:
            violations.append("committed_transition_not_public_success")
        if evidence.reconciliation_retained:
            violations.append("committed_transition_retained_reconciliation")
    else:
        if 200 <= evidence.public_status < 300:
            violations.append("incomplete_transition_reported_success")
        if not evidence.reconciliation_retained:
            violations.append("incomplete_transition_lost_reconciliation")
        if evidence.commit_record_durable:
            violations.append("transition_finalized_before_target")
    return _result(violations)
