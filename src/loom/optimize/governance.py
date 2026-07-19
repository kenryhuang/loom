"""Composition of optimized candidates with Loom's governed registry."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loom.campaigns.contracts import ApprovalRecord, ArtifactRef, PromotionDecision, PromotionDisposition
from loom.campaigns.materialization import CompiledDeclarativeCandidate
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, prefixed_id_from_digest, utc_now
from loom.core import ActorAssertion, Result, StaticIdentityProvider, err, make_loom_error, ok, thaw_json
from loom.governance.gates import GateEvidence
from loom.governance.policy import GovernanceReview
from loom.governance.promotion import (
    GovernedPromotionController,
    PromotionRequest,
    publish_candidate_evidence,
    publish_gate_evidence,
    publish_gate_source_evidence,
)
from loom.governance.registry import MonitorRegistration, SQLiteGovernanceStore


@dataclass(frozen=True, slots=True)
class LocalGovernanceActors:
    identity_provider: StaticIdentityProvider
    finalizer: ActorAssertion
    controller: ActorAssertion
    automation: ActorAssertion
    operator: ActorAssertion
    administrator: ActorAssertion
    approver: ActorAssertion


@dataclass(frozen=True, slots=True)
class GovernanceInputs:
    campaign_id: str
    candidate_id: str
    surface_id: str
    source_candidate_ref: ArtifactRef
    recommendation_ref: ArtifactRef
    compiled: CompiledDeclarativeCandidate
    baseline_ref: ArtifactRef
    supporting_evidence: tuple[ArtifactRef, ...]
    allowed_surfaces: tuple[str, ...]
    minimum_improvement: float
    minimum_sample_size: int = 3
    regression_threshold: float = 0.05
    ttl_runs: int = 20
    heartbeat_grace_seconds: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "allowed_surfaces", tuple(self.allowed_surfaces))
        if not self.supporting_evidence:
            raise ValueError("Governance inputs require supporting evidence")

    def as_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class GovernanceOutcome:
    decision: PromotionDecision
    decision_ref: ArtifactRef
    governed_candidate_ref: ArtifactRef
    policy_ref: ArtifactRef
    risk_rules_ref: ArtifactRef
    gate_evidence_ref: ArtifactRef
    monitor: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _PreparedGovernance:
    governed_candidate: ArtifactRef
    candidate_evidence: ArtifactRef
    policy: ArtifactRef
    risk_rules: ArtifactRef
    gate_evidence: ArtifactRef
    monitor: MonitorRegistration


def bootstrap_local_governance(output_dir: str | Path) -> Result:
    issued = "2026-01-01T00:00:00.000000Z"
    expires = "2999-01-01T00:00:00.000000Z"

    def actor(subject: str, role: str) -> ActorAssertion:
        signature = "local-" + canonical_digest({"subject": subject, "role": role})
        return ActorAssertion(subject, (role,), issued, expires, signature)

    assertions = (
        actor("local-campaign-finalizer", "campaign_finalizer"),
        actor("local-campaign-controller", "campaign_controller"),
        actor("local-governance-automation", "governance_automation"),
        actor("local-registry-operator", "registry_operator"),
        actor("local-governance-admin", "governance_admin"),
        actor("local-governance-approver", "governance_approver"),
    )
    values = LocalGovernanceActors(StaticIdentityProvider(assertions), *assertions)
    public = {
        "schema_version": "loom.local-governance-identities.v1",
        "identities": tuple({"subject": item.subject, "roles": item.roles, "issued_at": item.issued_at, "expires_at": item.expires_at} for item in assertions),
    }
    path = Path(output_dir) / "governance-identities.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, canonical_json_bytes(public) + b"\n")
    except OSError as exc:
        return _governance_error("LOCAL_IDENTITY_FAILED", "Could not write local identity metadata", cause=exc)
    return ok(values)


def load_production_governance(environment: dict[str, str] | None = None) -> Result:
    env = os.environ if environment is None else environment
    bindings = (
        ("LOOM_IDENTITY_CAMPAIGN_FINALIZER", "campaign_finalizer"),
        ("LOOM_IDENTITY_CAMPAIGN_CONTROLLER", "campaign_controller"),
        ("LOOM_IDENTITY_GOVERNANCE_AUTOMATION", "governance_automation"),
        ("LOOM_IDENTITY_REGISTRY_OPERATOR", "registry_operator"),
        ("LOOM_IDENTITY_GOVERNANCE_ADMIN", "governance_admin"),
        ("LOOM_IDENTITY_GOVERNANCE_APPROVER", "governance_approver"),
    )
    assertions = []
    try:
        for variable, required_role in bindings:
            path = env.get(variable)
            if not path:
                raise ValueError(f"missing identity reference: {variable}")
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            assertion = ActorAssertion(**payload)
            if assertion.roles != (required_role,):
                raise ValueError(f"identity {variable} must contain only role {required_role}")
            assertions.append(assertion)
        if len({item.subject for item in assertions}) != len(assertions) or len({item.signature for item in assertions}) != len(assertions):
            raise ValueError("production governance identities must be distinct")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _governance_error("GOVERNANCE_IDENTITY_INVALID", "Production governance identities are invalid", cause=exc)
    return ok(LocalGovernanceActors(StaticIdentityProvider(tuple(assertions)), *assertions))


class OptimizeGovernanceComposer:
    def __init__(self, artifacts, store_root: str | Path, actors: LocalGovernanceActors):
        self.artifacts = artifacts
        self.actors = actors
        self.store = SQLiteGovernanceStore(store_root, actors.identity_provider, artifacts)

    async def promote(self, inputs: GovernanceInputs) -> Result:
        prepared = await self._prepare(inputs)
        if not prepared.ok:
            return prepared
        approval = await self._matching_approval(inputs, prepared.value)
        if not approval.ok:
            return approval
        actor = self.actors.operator if approval.value is not None else self.actors.automation
        operation_kind = "approved" if approval.value is not None else "automatic"
        operation_id = prefixed_id_from_digest(
            "op_",
            canonical_digest(
                {
                    "campaign_id": inputs.campaign_id,
                    "candidate_id": inputs.candidate_id,
                    "surface": inputs.surface_id,
                    "kind": operation_kind,
                }
            ),
        )
        request = PromotionRequest(
            inputs.campaign_id,
            inputs.candidate_id,
            inputs.surface_id,
            prepared.value.candidate_evidence,
            inputs.baseline_ref,
            inputs.baseline_ref.sha256,
            prepared.value.policy,
            prepared.value.gate_evidence,
            approval.value,
            operation_id,
            utc_now(),
        )
        controller = GovernedPromotionController(self.store, self.artifacts, prepared.value.risk_rules)
        promoted = await controller.promote(request, actor, prepared.value.monitor)
        if not promoted.ok:
            return promoted
        decision_ref = self.artifacts.publish_bytes(
            canonical_json_bytes(promoted.value),
            kind="promotion_decision",
            schema_version="loom.promotion-decision.v1",
            suffix=".json",
        )
        if not decision_ref.ok:
            return decision_ref
        monitor = None
        if promoted.value.decision is PromotionDisposition.PROMOTED:
            monitors = await self.store.monitors("active")
            if not monitors.ok:
                return monitors
            monitor = next((item for item in monitors.value if item["monitor_id"] == prepared.value.monitor.monitor_id), None)
            if monitor is None:
                return _governance_error("MONITOR_MISSING", "Promoted candidate has no active monitor")
        return ok(
            GovernanceOutcome(
                promoted.value,
                decision_ref.value,
                prepared.value.governed_candidate,
                prepared.value.policy,
                prepared.value.risk_rules,
                prepared.value.gate_evidence,
                monitor,
            )
        )

    async def record_approval(
        self,
        inputs: GovernanceInputs,
        pending: PromotionDecision,
        *,
        rationale: str | None = None,
    ) -> Result:
        if pending.decision is not PromotionDisposition.AWAITING_APPROVAL or pending.candidate_id != inputs.candidate_id:
            return _governance_error("APPROVAL_INVALID", "Approval requires the matching awaiting-approval decision")
        prepared = await self._prepare(inputs)
        if not prepared.ok:
            return prepared
        created = datetime.now(UTC)
        approval = ApprovalRecord(
            self.actors.approver.subject,
            "governance_approver",
            created.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            (created + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            prepared.value.governed_candidate.sha256,
            inputs.baseline_ref.sha256,
            prepared.value.policy.sha256,
            prepared.value.risk_rules.sha256,
            canonical_digest(pending.gates),
            "approve",
            rationale,
        )
        review = GovernanceReview(
            prefixed_id_from_digest("review_", canonical_digest({"candidate": inputs.candidate_id, "approval": approval})),
            inputs.candidate_id,
            self.actors.approver,
            approval,
        )
        recorded = await self.store.record_review(review, approval.gate_digest)
        return ok(approval) if recorded.ok else recorded

    async def _prepare(self, inputs: GovernanceInputs) -> Result:
        operations = tuple(thaw_json(item) for item in inputs.compiled.operations)
        if not operations or any(not isinstance(item, dict) or not str(item.get("path", "")).startswith(f"{inputs.surface_id}.") for item in operations):
            return _governance_error("CANDIDATE_PATCH_INVALID", "Compiled candidate does not match its declared surface")
        target = inputs.compiled.materialized.get(inputs.surface_id)
        if target is None:
            return _governance_error("CANDIDATE_PATCH_INVALID", "Compiled candidate materialization omits its declared surface")
        governed = self.artifacts.publish_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "loom.governed-candidate.v1",
                    "campaign_id": inputs.campaign_id,
                    "candidate_id": inputs.candidate_id,
                    "source_candidate_ref": inputs.source_candidate_ref,
                    "kind": "declarative_patch",
                    "operations": operations,
                    "capability_manifest": {
                        "imports": (),
                        "dependencies": (),
                        "filesystem_read_roots": (),
                        "filesystem_write_roots": (),
                        "callable_tools": (),
                        "input_schema": {},
                        "output_schema": {},
                        "resource_limits": {},
                    },
                    "materialized": {inputs.surface_id: thaw_json(target)},
                }
            ),
            kind="governed_candidate",
            schema_version="loom.governed-candidate.v1",
            suffix=".json",
        )
        if not governed.ok:
            return governed
        policy = self.artifacts.publish_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "loom.governance-policy.v1",
                    "promotion": {
                        "allowed_surfaces": inputs.allowed_surfaces,
                        "auto_promote_max_risk": "low",
                        "executable_requires_human": True,
                        "require_holdout": True,
                        "minimum_improvement": inputs.minimum_improvement,
                    },
                    "monitoring": {
                        "minimum_sample_size": inputs.minimum_sample_size,
                        "quality_regression_threshold": inputs.regression_threshold,
                        "ttl_runs": inputs.ttl_runs,
                        "heartbeat_grace_seconds": inputs.heartbeat_grace_seconds,
                        "resource_ceilings": {},
                    },
                }
            ),
            kind="policy",
            schema_version="loom.governance-policy.v1",
            suffix=".json",
        )
        if not policy.ok:
            return policy
        rules = self.artifacts.publish_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "loom.risk-rule-set.v1",
                    "implementation_version": "loom.default-risk-rules.v1",
                }
            ),
            kind="risk_rules",
            schema_version="loom.risk-rule-set.v1",
            suffix=".json",
        )
        if not rules.ok:
            return rules
        for ref, kind in ((policy.value, "policy"), (rules.value, "risk_rules")):
            authorized = await self.store.authorize_governance_artifact(ref, kind, self.actors.administrator)
            if not authorized.ok:
                return authorized
        active = await self.store.active(inputs.surface_id)
        if not active.ok and active.error.code == "REGISTRY_NOT_FOUND":
            active = await self.store.initialize_surface(inputs.surface_id, inputs.baseline_ref, self.actors.administrator)
        if not active.ok:
            return active
        candidate = publish_candidate_evidence(
            self.artifacts,
            inputs.campaign_id,
            inputs.candidate_id,
            governed.value,
            inputs.recommendation_ref,
            self.actors.finalizer,
            self.actors.identity_provider,
        )
        if not candidate.ok:
            return candidate
        source = publish_gate_source_evidence(
            self.artifacts,
            inputs.campaign_id,
            inputs.candidate_id,
            {
                "task_set_isolation": True,
                "contamination_free": True,
                "evaluator_independent": True,
                "sandbox_conformant": True,
                "security_regression_passed": True,
            },
            inputs.supporting_evidence,
            self.actors.controller,
            self.actors.identity_provider,
        )
        if not source.ok:
            return source
        gate = publish_gate_evidence(
            self.artifacts,
            GateEvidence(
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                0,
                inputs.minimum_improvement,
                inputs.minimum_improvement,
                True,
                (source.value,),
            ),
            inputs.campaign_id,
            inputs.candidate_id,
            candidate.value,
            self.actors.finalizer,
            self.actors.identity_provider,
        )
        if not gate.ok:
            return gate
        monitor = MonitorRegistration(
            prefixed_id_from_digest("monitor_", canonical_digest({"candidate": inputs.candidate_id, "surface": inputs.surface_id})),
            inputs.surface_id,
            "pending",
            True,
            active.value.version,
            inputs.minimum_sample_size,
            inputs.regression_threshold,
            inputs.ttl_runs,
            inputs.heartbeat_grace_seconds,
            policy.value.sha256,
            {},
        )
        return ok(_PreparedGovernance(governed.value, candidate.value, policy.value, rules.value, gate.value, monitor))

    async def _matching_approval(self, inputs: GovernanceInputs, prepared: _PreparedGovernance) -> Result:
        pending_operation = prefixed_id_from_digest(
            "op_",
            canonical_digest(
                {
                    "campaign_id": inputs.campaign_id,
                    "candidate_id": inputs.candidate_id,
                    "surface": inputs.surface_id,
                    "kind": "automatic",
                }
            ),
        )
        pending = await self.store.operation_decision(pending_operation, inputs.surface_id)
        if not pending.ok:
            return pending
        expected_gate = None if pending.value is None else canonical_digest(pending.value.gates)
        reviews = await self.store.reviews(inputs.candidate_id)
        if not reviews.ok:
            return reviews
        for value in reversed(reviews.value):
            try:
                approval = ApprovalRecord(**value["approval"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                approval.decision == "approve"
                and approval.candidate_digest == prepared.governed_candidate.sha256
                and approval.baseline_digest == inputs.baseline_ref.sha256
                and approval.policy_digest == prepared.policy.sha256
                and approval.risk_rules_digest == prepared.risk_rules.sha256
                and expected_gate is not None
                and approval.gate_digest == expected_gate
            ):
                return ok(approval)
        return ok(None)


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _governance_error(code: str, message: str, *, cause: BaseException | None = None) -> Result:
    return err(
        make_loom_error(
            code,
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
        )
    )


__all__ = [
    "GovernanceInputs",
    "GovernanceOutcome",
    "LocalGovernanceActors",
    "OptimizeGovernanceComposer",
    "bootstrap_local_governance",
    "load_production_governance",
]
