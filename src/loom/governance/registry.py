"""Transactional governance registry, decisions, reviews, and monitors."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loom.campaigns.contracts import (
    ApprovalRecord,
    ArtifactRef,
    GateDecision,
    GateResult,
    PromotionDecision,
    PromotionDisposition,
    RiskLevel,
)
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, new_prefixed_id, utc_now
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok
from loom.governance.policy import ActorAssertion, GovernanceOperation, GovernanceReview, ReviewDecision, StaticIdentityProvider


@dataclass(frozen=True, slots=True)
class ActiveVersion:
    surface_id: str
    version: int
    artifact: ArtifactRef
    prior_artifact: ArtifactRef | None
    decision_id: str | None


@dataclass(frozen=True, slots=True)
class GovernanceLease:
    lease_id: str
    operation_id: str
    surface_id: str
    actor: ActorAssertion
    expected_active_version: int
    action: str = "activate"


@dataclass(frozen=True, slots=True)
class MonitorRegistration:
    monitor_id: str
    surface_id: str
    status: str
    healthy: bool
    expected_active_version: int
    minimum_sample_size: int
    regression_threshold: float
    ttl_runs: int = 20
    heartbeat_grace_seconds: int = 60
    policy_digest: str = ""
    resource_ceilings: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        frozen = freeze_json(self.resource_ceilings)
        if not isinstance(frozen, FrozenDict):
            raise TypeError("Monitor resource ceilings must be a mapping")
        object.__setattr__(self, "resource_ceilings", frozen)
        if self.minimum_sample_size < 1 or self.ttl_runs < 1 or self.heartbeat_grace_seconds < 1:
            raise ValueError("Monitor sample, TTL, and heartbeat limits must be positive")
        if (
            isinstance(self.regression_threshold, bool)
            or not isinstance(self.regression_threshold, int | float)
            or not math.isfinite(self.regression_threshold)
            or self.regression_threshold < 0
        ):
            raise ValueError("Monitor regression threshold must be finite and non-negative")
        if any(isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0 for value in frozen.values()):
            raise ValueError("Monitor resource ceilings must be finite non-negative numbers")


@dataclass(frozen=True, slots=True)
class MonitoringAction:
    monitor_id: str
    action: str
    active_version: int


class SQLiteGovernanceStore:
    def __init__(
        self,
        root: str | os.PathLike[str],
        identity_provider: StaticIdentityProvider,
        artifacts,
        *,
        failpoint: Callable[[str], bool] | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "governance.sqlite"
        self.identity_provider = identity_provider
        self.artifacts = artifacts
        self.failpoint = failpoint
        self._initialize()

    async def initialize_surface(self, surface_id: str, artifact: ArtifactRef, actor: ActorAssertion | None = None) -> Result:
        if actor is None:
            return _registry_error("AUTHENTICATION_FAILED", "Registry initialization requires a governance administrator")
        authorized = self.identity_provider.authorize(
            actor,
            "governance_admin",
            forbidden_roles=("governance_automation", "registry_operator", "governance_approver", "campaign_finalizer"),
        )
        if not authorized.ok:
            return authorized
        verified = self.artifacts.read_bytes(artifact)
        if not verified.ok:
            return verified
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO active(surface_id, version, artifact_json, prior_artifact_json, decision_id) VALUES (?, 0, ?, NULL, NULL)",
                (surface_id, _artifact_json(artifact)),
            )
            connection.execute(
                "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'registry.initialized', ?, ?)",
                (
                    new_prefixed_id("evt_"),
                    surface_id,
                    utc_now(),
                    json.dumps({"artifact": artifact.sha256, "administrator": actor.subject}),
                ),
            )
            connection.commit()
            return await self.active(surface_id)
        except sqlite3.IntegrityError:
            connection.rollback()
            return _registry_error("REGISTRY_CONFLICT", "Governance surface is already initialized")
        finally:
            connection.close()

    async def authorize_governance_artifact(self, ref: ArtifactRef, authority_kind: str, actor: ActorAssertion) -> Result:
        if authority_kind not in {"policy", "risk_rules"}:
            return _registry_error("GOVERNANCE_FAILED", "Governance authority artifact kind is not supported")
        authorized = self.identity_provider.authorize(
            actor,
            "governance_admin",
            forbidden_roles=("governance_automation", "registry_operator", "governance_approver", "campaign_finalizer"),
        )
        if not authorized.ok:
            return authorized
        verified = self.artifacts.read_bytes(ref)
        if not verified.ok:
            return verified
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT ref_json, administrator FROM authority_artifacts WHERE digest = ? AND authority_kind = ?",
                (ref.sha256, authority_kind),
            ).fetchone()
            ref_json = _artifact_json(ref)
            if existing is not None:
                connection.rollback()
                if existing != (ref_json, actor.subject):
                    return _registry_error("OPERATION_CONFLICT", "Governance artifact authorization conflicts with an existing record")
                return ok(ref)
            connection.execute(
                "INSERT INTO authority_artifacts(digest, authority_kind, ref_json, administrator, authorized_at) VALUES (?, ?, ?, ?, ?)",
                (ref.sha256, authority_kind, ref_json, actor.subject, utc_now()),
            )
            connection.execute(
                "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'governance.artifact_authorized', ?, ?)",
                (
                    new_prefixed_id("evt_"),
                    f"authority:{authority_kind}",
                    utc_now(),
                    json.dumps({"digest": ref.sha256, "administrator": actor.subject}),
                ),
            )
            connection.commit()
            return ok(ref)
        except sqlite3.Error as exc:
            connection.rollback()
            return err(make_loom_error("GOVERNANCE_FAILED", "Governance artifact authorization failed", retryable=False, cause=str(exc)))
        finally:
            connection.close()

    async def require_authorized_governance_artifact(self, ref: ArtifactRef, authority_kind: str) -> Result:
        verified = self.artifacts.read_bytes(ref)
        if not verified.ok:
            return verified
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT ref_json FROM authority_artifacts WHERE digest = ? AND authority_kind = ?",
                (ref.sha256, authority_kind),
            ).fetchone()
            if row is None or row[0] != _artifact_json(ref):
                return _registry_error("AUTHORIZATION_FAILED", "Governance artifact is not administrator-authorized")
            return ok(ref)
        finally:
            connection.close()

    async def active(self, surface_id: str) -> Result:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT version, artifact_json, prior_artifact_json, decision_id FROM active WHERE surface_id = ?",
                (surface_id,),
            ).fetchone()
            if row is None:
                return _registry_error("REGISTRY_NOT_FOUND", "Governance surface is not initialized")
            active = _artifact_from_json(row[1])
            prior = _optional_artifact(row[2])
            verified = self._verify_artifacts(active, *(() if prior is None else (prior,)))
            if not verified.ok:
                return verified
            return ok(ActiveVersion(surface_id, row[0], active, prior, row[3]))
        finally:
            connection.close()

    async def record_review(self, review: GovernanceReview, expected_gate_digest: str) -> Result:
        authorized = self.identity_provider.authorize(
            review.actor,
            "governance_approver",
            forbidden_roles=("campaign_finalizer", "candidate_author", "registry_operator"),
        )
        if not authorized.ok:
            return authorized
        approval = review.approval
        if approval.gate_digest != expected_gate_digest:
            return _registry_error("GATE_DIGEST_CONFLICT", "Review gate digest is stale")
        if approval.subject != review.actor.subject or approval.role != "governance_approver":
            return _registry_error("AUTHORIZATION_FAILED", "Review approval identity does not match the authenticated actor")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO reviews(review_id, candidate_id, subject, decision, gate_digest, candidate_digest, baseline_digest, "
                "policy_digest, risk_rules_digest, expires_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    review.candidate_id,
                    review.actor.subject,
                    approval.decision,
                    approval.gate_digest,
                    approval.candidate_digest,
                    approval.baseline_digest,
                    approval.policy_digest,
                    approval.risk_rules_digest,
                    approval.expires_at,
                    json.dumps(asdict(review), sort_keys=True),
                ),
            )
            connection.execute(
                "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'governance.reviewed', ?, ?)",
                (
                    new_prefixed_id("evt_"),
                    "candidate:" + review.candidate_id,
                    utc_now(),
                    json.dumps({"review_id": review.review_id, "decision": approval.decision, "gate_digest": approval.gate_digest}),
                ),
            )
            connection.commit()
            return ok(ReviewDecision(review.review_id, review.candidate_id, review.actor.subject, approval.decision, approval.gate_digest))
        except sqlite3.IntegrityError:
            connection.rollback()
            return _registry_error("REVIEW_CONFLICT", "Review ID is already used")
        finally:
            connection.close()

    async def acquire_lease(self, operation: GovernanceOperation, expected_active_version: int) -> Result:
        required = "registry_operator" if operation.action in {"activate", "rollback", "expire"} else "governance_automation"
        authorized = self.identity_provider.authorize(operation.actor, required)
        if not authorized.ok and operation.action == "activate":
            authorized = self.identity_provider.authorize(operation.actor, "governance_automation")
        if not authorized.ok:
            return authorized
        current = await self.active(operation.surface_id)
        if not current.ok:
            return current
        lease = GovernanceLease(
            new_prefixed_id("lease_"),
            operation.operation_id,
            operation.surface_id,
            operation.actor,
            expected_active_version,
            operation.action,
        )
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO leases(lease_id, operation_id, surface_id, actor_json, expected_version, action, status) VALUES (?, ?, ?, ?, ?, ?, 'active')",
                (
                    lease.lease_id,
                    lease.operation_id,
                    lease.surface_id,
                    json.dumps(asdict(lease.actor), sort_keys=True),
                    expected_active_version,
                    operation.action,
                ),
            )
            connection.commit()
            return ok(lease)
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT lease_id, surface_id, actor_json, expected_version, action FROM leases WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            actor_json = json.dumps(asdict(operation.actor), sort_keys=True)
            if row is not None and (row[1], row[2], row[3], row[4]) == (
                operation.surface_id,
                actor_json,
                expected_active_version,
                operation.action,
            ):
                return ok(GovernanceLease(row[0], operation.operation_id, row[1], operation.actor, row[3], row[4]))
            return _registry_error("OPERATION_CONFLICT", "Governance operation ID is already leased with different input")
        finally:
            connection.close()

    async def activate(self, lease: GovernanceLease, decision: PromotionDecision, monitor: MonitorRegistration) -> Result:
        activation_digest = canonical_digest({"decision": decision, "monitor": monitor})
        if not monitor.healthy:
            return _registry_error("MONITOR_UNHEALTHY", "Promotion monitor is not healthy")
        if monitor.surface_id != lease.surface_id or monitor.expected_active_version != lease.expected_active_version:
            return _registry_error("MONITOR_BINDING_INVALID", "Promotion monitor is not bound to the leased surface and version")
        executable = "executable_component" in decision.matched_risk_rules
        manual_activation = decision.human_approval is not None or decision.computed_risk.value != "low" or executable
        if manual_activation:
            authorized = self.identity_provider.authorize(
                lease.actor,
                "registry_operator",
                forbidden_roles=("governance_approver", "campaign_finalizer", "candidate_author"),
            )
        else:
            authorized = self.identity_provider.authorize(
                lease.actor,
                "governance_automation",
                forbidden_roles=("governance_approver", "campaign_finalizer", "candidate_author"),
            )
            if not authorized.ok:
                # A registry operator may always choose the explicit/manual path;
                # governance automation itself remains confined to low risk.
                authorized = self.identity_provider.authorize(
                    lease.actor,
                    "registry_operator",
                    forbidden_roles=("governance_approver", "campaign_finalizer", "candidate_author"),
                )
        if not authorized.ok:
            return authorized
        if decision.human_approval is not None and decision.human_approval.subject == lease.actor.subject:
            return _registry_error("AUTHORIZATION_FAILED", "Candidate approver cannot activate the same candidate")
        if decision.active_artifact is None or decision.rollback_artifact is None:
            return _registry_error("GOVERNANCE_FAILED", "Promotion requires active and rollback artifacts")
        if decision.decision is not PromotionDisposition.PROMOTED:
            return _registry_error("GOVERNANCE_FAILED", "Only promoted decisions may be activated")
        if decision.computed_risk.value == "forbidden" or (decision.computed_risk.value == "high" and not executable):
            return _registry_error("GOVERNANCE_FAILED", "Forbidden and high-risk declarative candidates cannot be activated")
        if (executable or decision.computed_risk.value == "medium") and decision.human_approval is None:
            return _registry_error("GOVERNANCE_FAILED", "Medium-risk and executable candidates require an independent human approval")
        if any(gate.mandatory and gate.result is not GateResult.PASSED for gate in decision.gates):
            return _registry_error("GOVERNANCE_FAILED", "Promotion decision contains a failed mandatory gate")
        evidence_refs = tuple(ref for gate in decision.gates for ref in gate.evidence_refs)
        verified = self._verify_artifacts(
            decision.active_artifact,
            decision.rollback_artifact,
            decision.risk_rule_set,
            *evidence_refs,
        )
        if not verified.ok:
            return verified
        approval_error = _validate_approval(decision, lease)
        if approval_error is not None:
            return approval_error
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease_row = connection.execute(
                "SELECT status, expected_version, action, input_digest, result_json, actor_json FROM leases "
                "WHERE lease_id = ? AND operation_id = ? AND surface_id = ?",
                (lease.lease_id, lease.operation_id, lease.surface_id),
            ).fetchone()
            active_row = connection.execute(
                "SELECT version, artifact_json FROM active WHERE surface_id = ?",
                (lease.surface_id,),
            ).fetchone()
            if lease_row is not None and lease_row[0] == "committed":
                connection.rollback()
                if lease_row[2] != "activate" or lease_row[3] != activation_digest or lease_row[4] is None:
                    return _registry_error("OPERATION_CONFLICT", "Committed activation replay input changed")
                return ok(_active_from_json(lease_row[4]))
            if (
                lease_row is None
                or lease_row[0] != "active"
                or lease_row[2] != "activate"
                or lease_row[5] != json.dumps(asdict(lease.actor), sort_keys=True)
                or lease_row[1] != lease.expected_active_version
            ):
                connection.rollback()
                return _registry_error("GOVERNANCE_LEASE_INVALID", "Governance lease is unavailable")
            if active_row is None or active_row[0] != lease.expected_active_version or active_row[0] != lease_row[1]:
                connection.rollback()
                return _registry_error("ACTIVE_VERSION_CONFLICT", "Active registry version changed")
            active_artifact = _artifact_from_json(active_row[1])
            if decision.baseline_digest != active_artifact.sha256 or decision.rollback_artifact.sha256 != active_artifact.sha256:
                connection.rollback()
                return _registry_error("GOVERNANCE_FAILED", "Baseline or rollback artifact does not match the active version")
            if decision.human_approval is not None:
                approval = decision.human_approval
                recorded = connection.execute(
                    "SELECT review_id FROM reviews WHERE candidate_id = ? AND subject = ? AND decision = 'approve' AND gate_digest = ? "
                    "AND candidate_digest = ? AND baseline_digest = ? AND policy_digest = ? AND risk_rules_digest = ? AND expires_at = ?",
                    (
                        decision.candidate_id,
                        approval.subject,
                        approval.gate_digest,
                        approval.candidate_digest,
                        approval.baseline_digest,
                        approval.policy_digest,
                        approval.risk_rules_digest,
                        approval.expires_at,
                    ),
                ).fetchone()
                if recorded is None:
                    connection.rollback()
                    return _registry_error("APPROVAL_STALE", "Approval has no matching authenticated governance review")
            new_version = active_row[0] + 1
            connection.execute(
                "INSERT INTO decisions(decision_id, surface_id, candidate_id, action, payload_json) VALUES (?, ?, ?, 'promoted', ?)",
                (decision.decision_id, lease.surface_id, decision.candidate_id, canonical_json_bytes(decision).decode()),
            )
            connection.execute(
                "UPDATE active SET version = ?, artifact_json = ?, prior_artifact_json = ?, decision_id = ? WHERE surface_id = ?",
                (new_version, _artifact_json(decision.active_artifact), active_row[1], decision.decision_id, lease.surface_id),
            )
            connection.execute(
                "INSERT INTO monitors(monitor_id, surface_id, observed_version, prior_artifact_json, status, healthy, "
                "minimum_samples, threshold, sample_count, delta_sum, ttl_runs, heartbeat_grace_seconds, last_heartbeat, policy_digest, "
                "resource_ceilings_json) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, 0, 0, ?, ?, ?, ?, ?)",
                (
                    monitor.monitor_id,
                    lease.surface_id,
                    new_version,
                    active_row[1],
                    monitor.minimum_sample_size,
                    monitor.regression_threshold,
                    monitor.ttl_runs,
                    monitor.heartbeat_grace_seconds,
                    utc_now(),
                    monitor.policy_digest,
                    canonical_json_bytes(monitor.resource_ceilings).decode(),
                ),
            )
            result = ActiveVersion(
                lease.surface_id,
                new_version,
                decision.active_artifact,
                _artifact_from_json(active_row[1]),
                decision.decision_id,
            )
            connection.execute(
                "UPDATE leases SET status = 'committed', input_digest = ?, result_json = ? WHERE lease_id = ?",
                (activation_digest, _active_json(result), lease.lease_id),
            )
            connection.execute(
                "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'promotion.activated', ?, ?)",
                (new_prefixed_id("evt_"), lease.surface_id, utc_now(), json.dumps({"decision_id": decision.decision_id, "version": new_version})),
            )
            if self.failpoint is not None and self.failpoint("before_commit"):
                raise RuntimeError("injected governance failpoint")
            connection.commit()
            return ok(result)
        except (sqlite3.Error, RuntimeError) as exc:
            connection.rollback()
            return err(
                make_loom_error(
                    "GOVERNANCE_FAILED",
                    "Atomic promotion failed",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )
        finally:
            connection.close()

    async def record_nonactivation_decision(
        self,
        operation: GovernanceOperation,
        expected_active_version: int,
        decision: PromotionDecision,
        monitor: MonitorRegistration,
    ) -> Result:
        """Persist a rejected or review-pending decision without moving a pointer."""
        if operation.action != "decide" or decision.decision is PromotionDisposition.PROMOTED:
            return _registry_error("GOVERNANCE_FAILED", "Non-activation recording requires a non-promoted decision")
        authorized = self.identity_provider.authorize(
            operation.actor,
            "governance_automation",
            forbidden_roles=("governance_approver", "campaign_finalizer", "candidate_author"),
        )
        if not authorized.ok:
            authorized = self.identity_provider.authorize(
                operation.actor,
                "registry_operator",
                forbidden_roles=("governance_approver", "campaign_finalizer", "candidate_author"),
            )
        if not authorized.ok:
            return authorized
        if decision.active_artifact is not None or decision.rollback_artifact is not None:
            return _registry_error("GOVERNANCE_FAILED", "Non-activation decisions cannot contain active registry artifacts")
        input_digest = canonical_digest({"decision": decision, "monitor": monitor})
        result_json = canonical_json_bytes(decision).decode()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT surface_id, actor_json, expected_version, action, status, input_digest, result_json FROM leases WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            actor_json = json.dumps(asdict(operation.actor), sort_keys=True)
            if replay is not None:
                connection.rollback()
                expected = (
                    operation.surface_id,
                    actor_json,
                    expected_active_version,
                    "decide",
                    "committed",
                    input_digest,
                    result_json,
                )
                if replay != expected:
                    return _registry_error("OPERATION_CONFLICT", "Governance decision replay input changed")
                return ok(decision)
            active = connection.execute(
                "SELECT version FROM active WHERE surface_id = ?",
                (operation.surface_id,),
            ).fetchone()
            if active is None:
                connection.rollback()
                return _registry_error("REGISTRY_NOT_FOUND", "Governance surface is not initialized")
            if active[0] != expected_active_version:
                connection.rollback()
                return _registry_error("ACTIVE_VERSION_CONFLICT", "Active registry version changed")
            connection.execute(
                "INSERT INTO leases(lease_id, operation_id, surface_id, actor_json, expected_version, action, input_digest, result_json, status) "
                "VALUES (?, ?, ?, ?, ?, 'decide', ?, ?, 'committed')",
                (
                    new_prefixed_id("lease_"),
                    operation.operation_id,
                    operation.surface_id,
                    actor_json,
                    expected_active_version,
                    input_digest,
                    result_json,
                ),
            )
            connection.execute(
                "INSERT INTO decisions(decision_id, surface_id, candidate_id, action, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    decision.decision_id,
                    operation.surface_id,
                    decision.candidate_id,
                    decision.decision.value,
                    result_json,
                ),
            )
            event_type = "promotion.rejected" if decision.decision is PromotionDisposition.REJECTED else "promotion.awaiting_approval"
            connection.execute(
                "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    new_prefixed_id("evt_"),
                    operation.surface_id,
                    event_type,
                    utc_now(),
                    json.dumps({"decision_id": decision.decision_id, "candidate_id": decision.candidate_id}),
                ),
            )
            if self.failpoint is not None and self.failpoint("before_commit"):
                raise RuntimeError("injected governance failpoint")
            connection.commit()
            return ok(decision)
        except (sqlite3.Error, RuntimeError) as exc:
            connection.rollback()
            return err(
                make_loom_error(
                    "GOVERNANCE_FAILED",
                    "Atomic governance decision recording failed",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )
        finally:
            connection.close()

    async def rollback(self, lease: GovernanceLease, *, reason: str) -> Result:
        authorized = self.identity_provider.authorize(lease.actor, "registry_operator")
        if not authorized.ok:
            return authorized
        connection = self._connect()
        rollback_digest = canonical_digest({"surface_id": lease.surface_id, "expected_version": lease.expected_active_version, "reason": reason})
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease_row = connection.execute(
                "SELECT status, expected_version, action, input_digest, result_json, actor_json FROM leases "
                "WHERE lease_id = ? AND operation_id = ? AND surface_id = ?",
                (lease.lease_id, lease.operation_id, lease.surface_id),
            ).fetchone()
            active_row = connection.execute(
                "SELECT version, artifact_json, prior_artifact_json FROM active WHERE surface_id = ?",
                (lease.surface_id,),
            ).fetchone()
            if lease_row is not None and lease_row[0] == "committed":
                connection.rollback()
                if lease_row[2] not in {"rollback", "expire"} or lease_row[3] != rollback_digest or lease_row[4] is None:
                    return _registry_error("OPERATION_CONFLICT", "Committed rollback replay input changed")
                return ok(_active_from_json(lease_row[4]))
            if (
                lease_row is None
                or lease_row[0] != "active"
                or lease_row[2] not in {"rollback", "expire"}
                or lease_row[5] != json.dumps(asdict(lease.actor), sort_keys=True)
                or lease_row[1] != lease.expected_active_version
            ):
                connection.rollback()
                return _registry_error("GOVERNANCE_LEASE_INVALID", "Governance lease is unavailable")
            if active_row is None or active_row[0] != lease.expected_active_version or active_row[0] != lease_row[1]:
                connection.rollback()
                return _registry_error("ACTIVE_VERSION_CONFLICT", "Active registry version changed")
            if active_row[2] is None:
                connection.rollback()
                return _registry_error("ROLLBACK_UNAVAILABLE", "Active version has no recorded prior artifact")
            new_version = active_row[0] + 1
            decision_id = new_prefixed_id("decision_")
            connection.execute(
                "UPDATE active SET version = ?, artifact_json = ?, prior_artifact_json = ?, decision_id = ? WHERE surface_id = ? AND version = ?",
                (new_version, active_row[2], active_row[1], decision_id, lease.surface_id, lease.expected_active_version),
            )
            result = ActiveVersion(
                lease.surface_id,
                new_version,
                _artifact_from_json(active_row[2]),
                _artifact_from_json(active_row[1]),
                decision_id,
            )
            connection.execute(
                "UPDATE leases SET status = 'committed', input_digest = ?, result_json = ? WHERE lease_id = ?",
                (rollback_digest, _active_json(result), lease.lease_id),
            )
            connection.execute("UPDATE monitors SET status = 'manual_rollback' WHERE surface_id = ? AND status = 'active'", (lease.surface_id,))
            connection.execute(
                "INSERT INTO decisions(decision_id, surface_id, candidate_id, action, payload_json) VALUES (?, ?, NULL, 'rolled_back', ?)",
                (decision_id, lease.surface_id, json.dumps({"reason": reason, "observed_version": lease.expected_active_version})),
            )
            connection.execute(
                "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'promotion.rolled_back', ?, ?)",
                (new_prefixed_id("evt_"), lease.surface_id, utc_now(), json.dumps({"decision_id": decision_id, "version": new_version})),
            )
            connection.commit()
            return ok(result)
        except sqlite3.Error as exc:
            connection.rollback()
            return err(make_loom_error("GOVERNANCE_FAILED", "Atomic rollback failed", retryable=False, cause=str(exc)))
        finally:
            connection.close()

    async def record_monitor_sample(
        self,
        monitor_id: str,
        observed_version: int,
        quality_delta: float,
        actor: ActorAssertion,
        resources: dict[str, float] | None = None,
    ) -> Result:
        authorized = self.identity_provider.authorize(
            actor,
            "monitor_service",
            forbidden_roles=("governance_automation", "registry_operator", "governance_approver"),
        )
        if not authorized.ok:
            return authorized
        resources = {} if resources is None else resources
        if not math.isfinite(quality_delta) or any(not math.isfinite(float(value)) for value in resources.values()):
            return _registry_error("GOVERNANCE_FAILED", "Monitoring evidence must contain only finite numeric values")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            monitor = connection.execute(
                "SELECT surface_id, observed_version, prior_artifact_json, status, minimum_samples, threshold, sample_count, delta_sum "
                ", ttl_runs, resource_ceilings_json FROM monitors WHERE monitor_id = ?",
                (monitor_id,),
            ).fetchone()
            if monitor is None or monitor[3] != "active":
                connection.rollback()
                return _registry_error("MONITOR_NOT_ACTIVE", "Monitor is not active")
            active = connection.execute("SELECT version, artifact_json FROM active WHERE surface_id = ?", (monitor[0],)).fetchone()
            if active is None:
                connection.rollback()
                return _registry_error("REGISTRY_NOT_FOUND", "Monitored surface is unavailable")
            if monitor[1] != observed_version:
                connection.rollback()
                return _registry_error("MONITOR_BINDING_INVALID", "Monitoring sample is bound to the wrong active version")
            if active[0] != observed_version:
                connection.execute("UPDATE monitors SET status = 'stale_closed' WHERE monitor_id = ?", (monitor_id,))
                connection.commit()
                return ok(MonitoringAction(monitor_id, "stale_closed", active[0]))
            count = monitor[6] + 1
            delta_sum = monitor[7] + quality_delta
            connection.execute("UPDATE monitors SET sample_count = ?, delta_sum = ? WHERE monitor_id = ?", (count, delta_sum, monitor_id))
            regression = count >= monitor[4] and delta_sum / count < -monitor[5]
            ttl_expired = count >= monitor[8]
            ceilings = json.loads(monitor[9])
            resource_breach = any(name in resources and float(resources[name]) > float(limit) for name, limit in ceilings.items())
            if regression or ttl_expired or resource_breach:
                connection.execute("UPDATE monitors SET status = 'rollback_requested' WHERE monitor_id = ?", (monitor_id,))
                connection.execute(
                    "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'promotion.rollback_requested', ?, ?)",
                    (
                        new_prefixed_id("evt_"),
                        monitor[0],
                        utc_now(),
                        json.dumps(
                            {
                                "monitor_id": monitor_id,
                                "observed_version": observed_version,
                                "reason": ("resource_ceiling" if resource_breach else "ttl_expired" if ttl_expired else "quality_regression"),
                            }
                        ),
                    ),
                )
                connection.commit()
                return ok(MonitoringAction(monitor_id, "rollback_requested", active[0]))
            connection.commit()
            return ok(MonitoringAction(monitor_id, "observed", active[0]))
        except sqlite3.Error as exc:
            connection.rollback()
            return err(make_loom_error("GOVERNANCE_FAILED", "Monitoring transaction failed", retryable=False, cause=str(exc)))
        finally:
            connection.close()

    async def heartbeat_monitor(self, monitor_id: str, observed_version: int, actor: ActorAssertion) -> Result:
        authorized = self.identity_provider.authorize(
            actor,
            "monitor_service",
            forbidden_roles=("governance_automation", "registry_operator", "governance_approver"),
        )
        if not authorized.ok:
            return authorized
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            monitor = connection.execute(
                "SELECT surface_id, observed_version, status FROM monitors WHERE monitor_id = ?",
                (monitor_id,),
            ).fetchone()
            if monitor is None or monitor[2] != "active":
                connection.rollback()
                return _registry_error("MONITOR_NOT_ACTIVE", "Monitor is not active")
            active = connection.execute("SELECT version FROM active WHERE surface_id = ?", (monitor[0],)).fetchone()
            if monitor[1] != observed_version or active is None or active[0] != observed_version:
                connection.rollback()
                return _registry_error("MONITOR_BINDING_INVALID", "Monitor heartbeat is bound to the wrong active version")
            connection.execute("UPDATE monitors SET last_heartbeat = ? WHERE monitor_id = ?", (utc_now(), monitor_id))
            connection.commit()
            return ok(MonitoringAction(monitor_id, "heartbeat", observed_version))
        except sqlite3.Error as exc:
            connection.rollback()
            return err(make_loom_error("GOVERNANCE_FAILED", "Monitor heartbeat failed", retryable=False, cause=str(exc)))
        finally:
            connection.close()

    async def sweep_stale_heartbeats(self, at: str, actor: ActorAssertion) -> Result:
        authorized = self.identity_provider.authorize(actor, "governance_automation")
        if not authorized.ok:
            return authorized
        try:
            observed_at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return _registry_error("GOVERNANCE_FAILED", "Heartbeat sweep timestamp is invalid")
        connection = self._connect()
        stale: list[tuple[str, int]] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT monitor_id, surface_id, observed_version, heartbeat_grace_seconds, last_heartbeat FROM monitors WHERE status = 'active'"
            ).fetchall()
            for monitor_id, surface_id, observed_version, grace_seconds, last_heartbeat in rows:
                heartbeat_at = datetime.fromisoformat(last_heartbeat.replace("Z", "+00:00"))
                if heartbeat_at + timedelta(seconds=grace_seconds) >= observed_at:
                    continue
                active = connection.execute("SELECT version FROM active WHERE surface_id = ?", (surface_id,)).fetchone()
                if active is None or active[0] != observed_version:
                    connection.execute("UPDATE monitors SET status = 'stale_closed' WHERE monitor_id = ?", (monitor_id,))
                    continue
                connection.execute("UPDATE monitors SET status = 'rollback_requested' WHERE monitor_id = ?", (monitor_id,))
                connection.execute(
                    "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'promotion.rollback_requested', ?, ?)",
                    (
                        new_prefixed_id("evt_"),
                        surface_id,
                        utc_now(),
                        json.dumps({"monitor_id": monitor_id, "observed_version": observed_version, "reason": "heartbeat_timeout"}),
                    ),
                )
                stale.append((monitor_id, observed_version))
            connection.commit()
        except (sqlite3.Error, ValueError) as exc:
            connection.rollback()
            return err(make_loom_error("GOVERNANCE_FAILED", "Heartbeat sweep failed", retryable=False, cause=str(exc)))
        finally:
            connection.close()
        actions = []
        for monitor_id, observed_version in stale:
            rolled_back = await self.execute_monitor_rollback(monitor_id, observed_version, actor)
            if not rolled_back.ok:
                return rolled_back
            actions.append(rolled_back.value)
        return ok(tuple(actions))

    async def execute_monitor_rollback(self, monitor_id: str, observed_version: int, actor: ActorAssertion) -> Result:
        authorized = self.identity_provider.authorize(actor, "governance_automation")
        if not authorized.ok:
            return authorized
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            monitor = connection.execute(
                "SELECT surface_id, observed_version, prior_artifact_json, status FROM monitors WHERE monitor_id = ?",
                (monitor_id,),
            ).fetchone()
            if monitor is None or monitor[3] != "rollback_requested":
                connection.rollback()
                return _registry_error("MONITOR_NOT_ACTIVE", "Monitor has no pending rollback request")
            active = connection.execute("SELECT version, artifact_json FROM active WHERE surface_id = ?", (monitor[0],)).fetchone()
            if monitor[1] != observed_version or active is None or active[0] != observed_version:
                connection.execute("UPDATE monitors SET status = 'stale_closed' WHERE monitor_id = ?", (monitor_id,))
                connection.commit()
                return ok(MonitoringAction(monitor_id, "stale_closed", -1 if active is None else active[0]))
            new_version = active[0] + 1
            decision_id = new_prefixed_id("decision_")
            updated = connection.execute(
                "UPDATE active SET version = ?, artifact_json = ?, prior_artifact_json = ?, decision_id = ? WHERE surface_id = ? AND version = ?",
                (new_version, monitor[2], active[1], decision_id, monitor[0], observed_version),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return _registry_error("ACTIVE_VERSION_CONFLICT", "Active registry version changed during rollback")
            connection.execute("UPDATE monitors SET status = 'rolled_back' WHERE monitor_id = ?", (monitor_id,))
            connection.execute(
                "INSERT INTO decisions(decision_id, surface_id, candidate_id, action, payload_json) VALUES (?, ?, NULL, 'rolled_back', ?)",
                (decision_id, monitor[0], json.dumps({"monitor_id": monitor_id, "observed_version": observed_version})),
            )
            connection.execute(
                "INSERT INTO events(event_id, surface_id, event_type, created_at, payload_json) VALUES (?, ?, 'promotion.rolled_back', ?, ?)",
                (new_prefixed_id("evt_"), monitor[0], utc_now(), json.dumps({"monitor_id": monitor_id, "version": new_version})),
            )
            connection.commit()
            return ok(MonitoringAction(monitor_id, "rolled_back", new_version))
        except sqlite3.Error as exc:
            connection.rollback()
            return err(make_loom_error("GOVERNANCE_FAILED", "Monitoring rollback failed", retryable=False, cause=str(exc)))
        finally:
            connection.close()

    async def audit(self, surface_id: str) -> Result:
        connection = self._connect()
        try:
            return ok(
                {
                    "decisions": connection.execute("SELECT COUNT(*) FROM decisions WHERE surface_id = ?", (surface_id,)).fetchone()[0],
                    "events": connection.execute("SELECT COUNT(*) FROM events WHERE surface_id = ?", (surface_id,)).fetchone()[0],
                    "active_monitors": connection.execute("SELECT COUNT(*) FROM monitors WHERE surface_id = ? AND status = 'active'", (surface_id,)).fetchone()[
                        0
                    ],
                }
            )
        finally:
            connection.close()

    async def active_versions(self, surface_id: str | None = None) -> Result:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT surface_id FROM active WHERE (? IS NULL OR surface_id = ?) ORDER BY surface_id",
                (surface_id, surface_id),
            ).fetchall()
        finally:
            connection.close()
        values = []
        for (name,) in rows:
            active = await self.active(name)
            if not active.ok:
                return active
            values.append(active.value)
        return ok(tuple(values))

    async def reviews(self, candidate_id: str) -> Result:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload_json FROM reviews WHERE candidate_id = ? ORDER BY review_id",
                (candidate_id,),
            ).fetchall()
            return ok(tuple(json.loads(row[0]) for row in rows))
        finally:
            connection.close()

    async def monitors(self, status: str | None = None) -> Result:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT monitor_id, surface_id, observed_version, status, healthy, minimum_samples, threshold, sample_count, "
                "ttl_runs, heartbeat_grace_seconds, last_heartbeat, policy_digest, resource_ceilings_json "
                "FROM monitors WHERE (? IS NULL OR status = ?) ORDER BY monitor_id",
                (status, status),
            ).fetchall()
            return ok(
                tuple(
                    {
                        "monitor_id": row[0],
                        "surface_id": row[1],
                        "observed_version": row[2],
                        "status": row[3],
                        "healthy": bool(row[4]),
                        "minimum_sample_size": row[5],
                        "regression_threshold": row[6],
                        "sample_count": row[7],
                        "ttl_runs": row[8],
                        "heartbeat_grace_seconds": row[9],
                        "last_heartbeat": row[10],
                        "policy_digest": row[11],
                        "resource_ceilings": json.loads(row[12]),
                    }
                    for row in rows
                )
            )
        finally:
            connection.close()

    async def active_for_decision(self, decision_id: str) -> Result:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT surface_id FROM active WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return _registry_error("REGISTRY_NOT_FOUND", "Promotion is not the active decision")
        return await self.active(row[0])

    async def operation_decision(self, operation_id: str, surface_id: str) -> Result:
        connection = self._connect()
        try:
            lease = connection.execute(
                "SELECT surface_id, status, action, result_json FROM leases WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if lease is None:
                return ok(None)
            if lease[0] != surface_id:
                return _registry_error("OPERATION_CONFLICT", "Governance operation belongs to another surface")
            if lease[1] != "committed" or lease[3] is None:
                return ok(None)
            if lease[2] == "decide":
                return ok(_promotion_from_json(lease[3]))
            if lease[2] != "activate":
                return ok(None)
            active_result = _active_from_json(lease[3])
            if active_result.decision_id is None:
                return _registry_error("GOVERNANCE_FAILED", "Committed activation result has no decision")
            row = connection.execute(
                "SELECT payload_json FROM decisions WHERE decision_id = ? AND action = 'promoted'",
                (active_result.decision_id,),
            ).fetchone()
            if row is None:
                return _registry_error("GOVERNANCE_FAILED", "Committed activation decision is unavailable")
            return ok(_promotion_from_json(row[0]))
        finally:
            connection.close()

    async def activation_decision(self, operation_id: str, surface_id: str) -> Result:
        """Backward-compatible alias for decision replay lookup."""
        return await self.operation_decision(operation_id, surface_id)

    async def verify_activation_replay(
        self,
        operation_id: str,
        surface_id: str,
        decision: PromotionDecision,
        monitor: MonitorRegistration,
    ) -> Result:
        expected_digest = canonical_digest({"decision": decision, "monitor": monitor})
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT surface_id, status, action, input_digest FROM leases WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row != (surface_id, "committed", "activate", expected_digest):
                return _registry_error("OPERATION_CONFLICT", "Promotion replay activation inputs changed")
            return ok(None)
        finally:
            connection.close()

    async def verify_decision_replay(
        self,
        operation_id: str,
        surface_id: str,
        decision: PromotionDecision,
        monitor: MonitorRegistration,
    ) -> Result:
        expected_digest = canonical_digest({"decision": decision, "monitor": monitor})
        expected_action = "activate" if decision.decision is PromotionDisposition.PROMOTED else "decide"
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT surface_id, status, action, input_digest FROM leases WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row != (surface_id, "committed", expected_action, expected_digest):
                return _registry_error("OPERATION_CONFLICT", "Governance decision replay inputs changed")
            return ok(None)
        finally:
            connection.close()

    def _verify_artifacts(self, *refs: ArtifactRef) -> Result:
        for ref in refs:
            verified = self.artifacts.read_bytes(ref)
            if not verified.ok:
                return verified
        return ok(None)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS active(
                    surface_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    artifact_json TEXT NOT NULL,
                    prior_artifact_json TEXT,
                    decision_id TEXT
                );
                CREATE TABLE IF NOT EXISTS leases(
                    lease_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    surface_id TEXT NOT NULL,
                    actor_json TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    action TEXT NOT NULL DEFAULT '',
                    input_digest TEXT,
                    result_json TEXT,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions(
                    decision_id TEXT PRIMARY KEY,
                    surface_id TEXT NOT NULL,
                    candidate_id TEXT,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews(
                    review_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    gate_digest TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    baseline_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    risk_rules_digest TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitors(
                    monitor_id TEXT PRIMARY KEY,
                    surface_id TEXT NOT NULL,
                    observed_version INTEGER NOT NULL,
                    prior_artifact_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    healthy INTEGER NOT NULL,
                    minimum_samples INTEGER NOT NULL,
                    threshold REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    delta_sum REAL NOT NULL
                    , ttl_runs INTEGER NOT NULL
                    , heartbeat_grace_seconds INTEGER NOT NULL
                    , last_heartbeat TEXT NOT NULL
                    , policy_digest TEXT NOT NULL
                    , resource_ceilings_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    surface_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authority_artifacts(
                    digest TEXT NOT NULL,
                    authority_kind TEXT NOT NULL,
                    ref_json TEXT NOT NULL,
                    administrator TEXT NOT NULL,
                    authorized_at TEXT NOT NULL,
                    PRIMARY KEY(digest, authority_kind)
                );
                """
            )
            _ensure_column(connection, "leases", "action", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "leases", "input_digest", "TEXT")
            _ensure_column(connection, "leases", "result_json", "TEXT")
        finally:
            connection.close()


def _artifact_json(value: ArtifactRef) -> str:
    return canonical_json_bytes(value).decode()


def _artifact_from_json(value: str) -> ArtifactRef:
    return ArtifactRef(**json.loads(value))


def _optional_artifact(value: str | None) -> ArtifactRef | None:
    return None if value is None else _artifact_from_json(value)


def _active_json(value: ActiveVersion) -> str:
    return canonical_json_bytes(value).decode()


def _active_from_json(value: str) -> ActiveVersion:
    payload = json.loads(value)
    return ActiveVersion(
        payload["surface_id"],
        payload["version"],
        ArtifactRef(**payload["artifact"]),
        None if payload["prior_artifact"] is None else ArtifactRef(**payload["prior_artifact"]),
        payload["decision_id"],
    )


def _promotion_from_json(value: str) -> PromotionDecision:
    payload = json.loads(value)
    gates = tuple(
        GateDecision(
            item["gate_id"],
            item["mandatory"],
            GateResult(item["result"]),
            tuple(ArtifactRef(**ref) for ref in item["evidence_refs"]),
            item["reason"],
        )
        for item in payload["gates"]
    )
    approval = None if payload["human_approval"] is None else ApprovalRecord(**payload["human_approval"])
    return PromotionDecision(
        payload["schema_version"],
        payload["decision_id"],
        payload["campaign_id"],
        payload["candidate_id"],
        payload["created_at"],
        payload["baseline_digest"],
        payload["policy_digest"],
        ArtifactRef(**payload["risk_rule_set"]),
        gates,
        RiskLevel(payload["computed_risk"]),
        tuple(payload["matched_risk_rules"]),
        approval,
        PromotionDisposition(payload["decision"]),
        None if payload["active_artifact"] is None else ArtifactRef(**payload["active_artifact"]),
        None if payload["rollback_artifact"] is None else ArtifactRef(**payload["rollback_artifact"]),
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _registry_error(code: str, message: str) -> Result:
    return err(make_loom_error(code, message, retryable=False))


def _validate_approval(decision: PromotionDecision, lease: GovernanceLease) -> Result | None:
    approval = decision.human_approval
    if approval is None:
        return None
    expected = {
        "candidate_digest": decision.active_artifact.sha256 if decision.active_artifact is not None else None,
        "baseline_digest": decision.baseline_digest,
        "policy_digest": decision.policy_digest,
        "risk_rules_digest": decision.risk_rule_set.sha256,
        "gate_digest": canonical_digest(decision.gates),
    }
    if approval.decision != "approve" or approval.role != "governance_approver":
        return _registry_error("APPROVAL_STALE", "Approval is not an approving governance review")
    if any(getattr(approval, field) != value for field, value in expected.items()):
        return _registry_error("APPROVAL_STALE", "Approval digests do not match the promotion decision")
    try:
        expires_at = datetime.fromisoformat(approval.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return _registry_error("APPROVAL_STALE", "Approval expiry timestamp is invalid")
    if expires_at <= datetime.now(UTC):
        return _registry_error("APPROVAL_STALE", "Approval has expired")
    if approval.subject == lease.actor.subject:
        return _registry_error("AUTHORIZATION_FAILED", "Candidate approver cannot activate the same candidate")
    return None


__all__ = [
    "ActiveVersion",
    "GovernanceLease",
    "MonitorRegistration",
    "MonitoringAction",
    "SQLiteGovernanceStore",
]
