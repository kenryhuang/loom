"""Governed promotion decisions and atomic activation orchestration."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

from loom.campaigns.contracts import (
    ApprovalRecord,
    ArtifactRef,
    CandidateKind,
    CapabilityManifest,
    PromotionDecision,
    PromotionDisposition,
)
from loom.campaigns.operations import CampaignOperation
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, prefixed_id_from_digest
from loom.core import Result, err, make_loom_error, ok
from loom.governance.gates import GateEvidence, evaluate_gates, promotion_disposition
from loom.governance.policy import ActorAssertion, GovernanceOperation
from loom.governance.registry import MonitorRegistration
from loom.governance.risk import RiskInput, classify_risk

_CONTROLLER_GATE_CHECKS = frozenset(
    {
        "task_set_isolation",
        "contamination_free",
        "evaluator_independent",
        "sandbox_conformant",
        "security_regression_passed",
    }
)


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    campaign_id: str
    candidate_id: str
    surface_id: str
    candidate_artifact: ArtifactRef
    rollback_artifact: ArtifactRef
    baseline_digest: str
    policy_artifact: ArtifactRef
    gate_evidence: ArtifactRef
    approval: ApprovalRecord | None
    operation_id: str
    requested_at: str


class GovernedPromotionController:
    def __init__(self, store, artifacts, risk_rule_set: ArtifactRef, *, campaign_store=None, campaign_actor=None):
        if (campaign_store is None) != (campaign_actor is None):
            raise ValueError("Campaign governance recording requires both store and actor")
        self.store = store
        self.artifacts = artifacts
        self.risk_rule_set = risk_rule_set
        self.campaign_store = campaign_store
        self.campaign_actor = campaign_actor

    async def promote(self, request: PromotionRequest, actor: ActorAssertion, monitor: MonitorRegistration) -> Result:
        replayed = await self.store.operation_decision(request.operation_id, request.surface_id)
        if not replayed.ok:
            return replayed
        replayed_decision = replayed.value
        active = None
        if replayed_decision is None:
            active = await self.store.active(request.surface_id)
            if not active.ok:
                return active
        if request.rollback_artifact.sha256 != request.baseline_digest or (active is not None and active.value.artifact.sha256 != request.baseline_digest):
            return _promotion_error("Promotion baseline or rollback digest is stale")
        for ref in (request.candidate_artifact, request.rollback_artifact, request.policy_artifact, request.gate_evidence, self.risk_rule_set):
            verified = self.artifacts.read_bytes(ref)
            if not verified.ok:
                return verified
        authorized_policy = await self.store.require_authorized_governance_artifact(request.policy_artifact, "policy")
        if not authorized_policy.ok:
            return authorized_policy
        authorized_rules = await self.store.require_authorized_governance_artifact(self.risk_rule_set, "risk_rules")
        if not authorized_rules.ok:
            return authorized_rules
        compatible_rules = _load_risk_rule_set(self.artifacts.read_bytes(self.risk_rule_set).value)
        if not compatible_rules.ok:
            return compatible_rules
        candidate_evidence = _load_candidate_evidence(
            self.artifacts,
            self.artifacts.read_bytes(request.candidate_artifact).value,
            request.campaign_id,
            request.candidate_id,
            self.store.identity_provider,
        )
        if not candidate_evidence.ok:
            return candidate_evidence
        risk_input, active_candidate_artifact, recommendation_ref, recommendation_payload = candidate_evidence.value
        evidence = _load_gate_evidence(
            self.artifacts,
            self.artifacts.read_bytes(request.gate_evidence).value,
            self.store.identity_provider,
            request.campaign_id,
            request.candidate_id,
            request.candidate_artifact,
            recommendation_ref,
        )
        if not evidence.ok:
            return evidence
        governance_policy = _load_governance_policy(self.artifacts.read_bytes(request.policy_artifact).value)
        if not governance_policy.ok:
            return governance_policy
        expected_monitor = governance_policy.value["monitoring"]
        if (
            monitor.minimum_sample_size != expected_monitor["minimum_sample_size"]
            or monitor.regression_threshold != expected_monitor["quality_regression_threshold"]
            or monitor.ttl_runs != expected_monitor["ttl_runs"]
            or monitor.heartbeat_grace_seconds != expected_monitor["heartbeat_grace_seconds"]
            or monitor.policy_digest != request.policy_artifact.sha256
            or dict(monitor.resource_ceilings) != expected_monitor["resource_ceilings"]
        ):
            return _promotion_error("Monitor registration does not match the frozen governance policy")
        for ref in evidence.value.evidence_refs:
            verified = self.artifacts.read_bytes(ref)
            if not verified.ok:
                return verified
        assessment = classify_risk(request.candidate_id, risk_input, self.risk_rule_set)
        promotion_policy = governance_policy.value["promotion"]
        changed_surfaces = frozenset(risk_input.changed_surfaces)
        if request.surface_id not in changed_surfaces or not changed_surfaces.issubset(promotion_policy["allowed_surfaces"]):
            return _promotion_error("Candidate surfaces are not admitted by the frozen governance policy")
        derived_gates = _derive_recommendation_gates(evidence.value, recommendation_payload, request.candidate_id)
        if not derived_gates.ok:
            return derived_gates
        evidence_value = replace(
            derived_gates.value,
            artifact_integrity=True,
            evidence_integrity=True,
            forbidden_surface_free=True,
            baseline_fresh=True,
            policy_fresh=True,
            rollback_verified=True,
            monitoring_ready=monitor.healthy,
            required_improvement=promotion_policy["minimum_improvement"],
            holdout_passed=derived_gates.value.holdout_passed if promotion_policy["require_holdout"] else True,
        )
        executable = risk_input.kind is CandidateKind.EXECUTABLE_COMPONENT
        gates = evaluate_gates(evidence_value, assessment.level, executable=executable)
        disposition = promotion_disposition(
            gates,
            assessment.level,
            executable=executable,
            approval=request.approval,
        )
        decision = PromotionDecision(
            "loom.promotion-decision.v1",
            prefixed_id_from_digest("decision_", canonical_digest({"operation_id": request.operation_id})),
            request.campaign_id,
            request.candidate_id,
            request.requested_at,
            request.baseline_digest,
            request.policy_artifact.sha256,
            self.risk_rule_set,
            gates,
            assessment.level,
            assessment.matched_rules,
            request.approval,
            disposition,
            active_candidate_artifact if disposition is PromotionDisposition.PROMOTED else None,
            request.rollback_artifact if disposition is PromotionDisposition.PROMOTED else None,
        )
        if replayed_decision is not None:
            if decision != replayed_decision:
                return err(make_loom_error("OPERATION_CONFLICT", "Promotion replay input changed", retryable=False))
            replay_match = await self.store.verify_decision_replay(request.operation_id, request.surface_id, decision, monitor)
            if not replay_match.ok:
                return replay_match
            recorded = await self._record_campaign_decision(decision)
            return ok(decision) if recorded.ok else recorded
        if disposition is not PromotionDisposition.PROMOTED:
            assert active is not None
            persisted = await self.store.record_nonactivation_decision(
                GovernanceOperation(request.operation_id, request.surface_id, actor, "decide"),
                active.value.version,
                decision,
                monitor,
            )
            if not persisted.ok:
                return persisted
            recorded = await self._record_campaign_decision(decision)
            return ok(decision) if recorded.ok else recorded
        assert active is not None
        operation = GovernanceOperation(request.operation_id, request.surface_id, actor, "activate")
        lease = await self.store.acquire_lease(operation, active.value.version)
        if not lease.ok:
            return lease
        activated = await self.store.activate(lease.value, decision, monitor)
        if not activated.ok:
            return activated
        recorded = await self._record_campaign_decision(decision)
        return ok(decision) if recorded.ok else recorded

    async def _record_campaign_decision(self, decision: PromotionDecision) -> Result:
        if self.campaign_store is None:
            return ok(None)
        published = self.campaign_store.artifacts.publish_bytes(
            canonical_json_bytes(decision),
            kind="governance_decision",
            schema_version=decision.schema_version,
            suffix=".json",
        )
        if not published.ok:
            return published
        projection = await self.campaign_store.load(decision.campaign_id)
        if not projection.ok:
            return projection
        if decision.decision is PromotionDisposition.PROMOTED:
            event_type = "candidate.promoted"
        elif decision.decision is PromotionDisposition.AWAITING_APPROVAL:
            event_type = "candidate.awaiting_approval"
        else:
            event_type = "promotion.rejected"
        operation = CampaignOperation(
            prefixed_id_from_digest("op_", canonical_digest({"event": event_type, "decision": decision.decision_id})),
            decision.campaign_id,
            canonical_digest(decision),
            event_type,
            actor=self.campaign_actor,
            payload={"candidate_id": decision.candidate_id, "governance_ref": published.value},
            output_refs=(published.value,),
        )
        return await self.campaign_store.transact(operation, projection.value.aggregate_version)


def publish_candidate_evidence(
    artifacts,
    campaign_id: str,
    candidate_id: str,
    candidate_artifact: ArtifactRef,
    recommendation_ref: ArtifactRef,
    actor: ActorAssertion,
    identity_provider,
) -> Result:
    authorized = identity_provider.authorize(
        actor,
        "campaign_finalizer",
        forbidden_roles=("governance_automation", "registry_operator", "governance_approver", "candidate_author"),
    )
    if not authorized.ok:
        return authorized
    recommendation = _verify_recommendation(artifacts, recommendation_ref, campaign_id, candidate_id, actor)
    if not recommendation.ok:
        return recommendation
    derived = _derive_candidate_risk_input(artifacts, candidate_artifact, campaign_id, candidate_id)
    if not derived.ok:
        return derived
    risk_input, source_candidate_ref = derived.value
    recommended_source = _recommendation_candidate_ref(recommendation.value)
    if not recommended_source.ok:
        return recommended_source
    if source_candidate_ref != recommended_source.value:
        return _promotion_error("Governed candidate is not derived from the recommended candidate artifact")
    claims = {
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_artifact": candidate_artifact,
        "source_candidate_ref": source_candidate_ref,
        "risk_input_digest": canonical_digest(risk_input),
        "recommendation_ref": recommendation_ref,
    }
    payload = {
        "schema_version": "loom.promotion-candidate.v1",
        "claims": claims,
        "attestation": {"actor": asdict(actor), "claims_digest": canonical_digest(claims)},
    }
    return artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="promotion_candidate",
        schema_version="loom.promotion-candidate.v1",
        suffix=".json",
    )


def publish_gate_evidence(
    artifacts,
    evidence: GateEvidence,
    campaign_id: str,
    candidate_id: str,
    candidate_evidence_ref: ArtifactRef,
    actor: ActorAssertion,
    identity_provider,
) -> Result:
    authorized = identity_provider.authorize(
        actor,
        "campaign_finalizer",
        forbidden_roles=("governance_automation", "registry_operator", "governance_approver", "candidate_author"),
    )
    if not authorized.ok:
        return authorized
    candidate_raw = artifacts.read_bytes(candidate_evidence_ref, expected_schema="loom.promotion-candidate.v1")
    if not candidate_raw.ok:
        return candidate_raw
    candidate_evidence = _load_candidate_evidence(artifacts, candidate_raw.value, campaign_id, candidate_id, identity_provider)
    if not candidate_evidence.ok:
        return candidate_evidence
    source_checks = _load_controller_gate_sources(
        artifacts,
        evidence.evidence_refs,
        campaign_id,
        candidate_id,
        identity_provider,
    )
    if not source_checks.ok:
        return source_checks
    _, _, recommendation_ref, recommendation_payload = candidate_evidence.value
    controller_derived = replace(evidence, **source_checks.value)
    derived = _derive_recommendation_gates(controller_derived, recommendation_payload, candidate_id)
    if not derived.ok:
        return derived
    claims = {
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_evidence_ref": candidate_evidence_ref,
        "recommendation_ref": recommendation_ref,
        "evidence": asdict(derived.value),
    }
    payload = {
        "schema_version": "loom.gate-evidence.v1",
        "claims": claims,
        "attestation": {
            "actor": asdict(actor),
            "claims_digest": canonical_digest(claims),
        },
    }
    return artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="gate_evidence",
        schema_version="loom.gate-evidence.v1",
        suffix=".json",
    )


def publish_gate_source_evidence(
    artifacts,
    campaign_id: str,
    candidate_id: str,
    checks: Mapping[str, bool],
    evidence_refs: tuple[ArtifactRef, ...],
    actor: ActorAssertion,
    identity_provider,
) -> Result:
    authorized = identity_provider.authorize(
        actor,
        "campaign_controller",
        forbidden_roles=("campaign_finalizer", "governance_automation", "registry_operator", "governance_approver", "candidate_author"),
    )
    if not authorized.ok:
        return authorized
    if set(checks) != _CONTROLLER_GATE_CHECKS or any(not isinstance(value, bool) for value in checks.values()):
        return _promotion_error("Controller gate source checks are incomplete or invalid")
    for ref in evidence_refs:
        verified = artifacts.read_bytes(ref)
        if not verified.ok:
            return verified
    claims = {
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "checks": dict(checks),
        "evidence_refs": tuple(evidence_refs),
    }
    payload = {
        "schema_version": "loom.gate-source.v1",
        "claims": claims,
        "attestation": {"actor": asdict(actor), "claims_digest": canonical_digest(claims)},
    }
    return artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="gate_source",
        schema_version="loom.gate-source.v1",
        suffix=".json",
    )


def _load_candidate_evidence(artifacts, raw: bytes, campaign_id: str, candidate_id: str, identity_provider) -> Result:
    try:
        payload = json.loads(raw)
        if payload.get("schema_version") != "loom.promotion-candidate.v1":
            raise ValueError("candidate evidence identity or schema does not match")
        claims = payload["claims"]
        attestation = payload["attestation"]
        actor = ActorAssertion(**attestation["actor"])
        authorized = identity_provider.authorize(
            actor,
            "campaign_finalizer",
            forbidden_roles=("governance_automation", "registry_operator", "governance_approver", "candidate_author"),
        )
        if not authorized.ok:
            return authorized
        if claims.get("campaign_id") != campaign_id or claims.get("candidate_id") != candidate_id or attestation["claims_digest"] != canonical_digest(claims):
            raise ValueError("candidate evidence identity or attestation does not match")
        recommendation_ref = ArtifactRef(**claims["recommendation_ref"])
        recommendation = _verify_recommendation(artifacts, recommendation_ref, campaign_id, candidate_id, actor)
        if not recommendation.ok:
            return recommendation
        candidate_ref = ArtifactRef(**claims["candidate_artifact"])
        derived = _derive_candidate_risk_input(artifacts, candidate_ref, campaign_id, candidate_id)
        if not derived.ok:
            return derived
        risk_input, source_candidate_ref = derived.value
        recommended_source = _recommendation_candidate_ref(recommendation.value)
        if not recommended_source.ok:
            return recommended_source
        claimed_source = ArtifactRef(**claims["source_candidate_ref"])
        if source_candidate_ref != recommended_source.value or claimed_source != source_candidate_ref:
            raise ValueError("governed candidate source does not match its recommendation")
        if claims.get("risk_input_digest") != canonical_digest(risk_input):
            raise ValueError("candidate risk derivation digest does not match")
    except (AttributeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _promotion_error("Promotion candidate evidence is invalid", cause=exc)
    return ok((risk_input, candidate_ref, recommendation_ref, recommendation.value))


def _derive_candidate_risk_input(artifacts, ref: ArtifactRef, campaign_id: str, candidate_id: str) -> Result:
    read = artifacts.read_bytes(ref, expected_schema="loom.governed-candidate.v1")
    if not read.ok:
        return read
    try:
        payload = json.loads(read.value)
        if (
            payload.get("schema_version") != "loom.governed-candidate.v1"
            or payload.get("campaign_id") != campaign_id
            or payload.get("candidate_id") != candidate_id
        ):
            raise ValueError("governed candidate identity or schema does not match")
        source_candidate_ref = ArtifactRef(**payload["source_candidate_ref"])
        source = artifacts.read_bytes(source_candidate_ref, expected_schema="loom.candidate-bundle.v1")
        if not source.ok:
            return source
        kind = CandidateKind(payload["kind"])
        operations = payload["operations"]
        if not isinstance(operations, list) or not operations:
            raise ValueError("governed candidate operations must be a non-empty list")
        surfaces: list[str] = []
        operation_names: list[str] = []
        for operation in operations:
            if not isinstance(operation, dict) or not isinstance(operation.get("op"), str) or not isinstance(operation.get("path"), str):
                raise TypeError("governed candidate operation is malformed")
            resolved = _resolve_materialized_surface_field(operation["path"], payload.get("materialized"))
            if resolved is None:
                raise ValueError("governed candidate operation path is invalid")
            surfaces.append(resolved[0])
            operation_names.append(operation["op"])
        materialized = payload["materialized"]
        if not isinstance(materialized, dict) or set(materialized) != set(surfaces):
            raise ValueError("governed candidate materialization must exactly cover its operation surfaces")
        for operation in operations:
            resolved = _resolve_materialized_surface_field(operation["path"], materialized)
            if resolved is None:
                raise ValueError("governed candidate operation path is invalid")
            surface, field = resolved
            target = materialized.get(surface)
            if not isinstance(target, dict) or field not in target:
                raise ValueError("governed candidate operation target is absent from its materialization")
            if operation["op"] in {"set", "set_limit", "replace"} and target[field] != operation.get("value"):
                raise ValueError("governed candidate operation value does not match its materialization")
            if operation["op"] == "append_rule" and (not isinstance(target[field], list) or not target[field] or target[field][-1] != operation.get("value")):
                raise ValueError("governed candidate append result does not match its materialization")
            if operation["op"] not in {"set", "set_limit", "replace", "append_rule"}:
                raise ValueError("governed candidate operation is not supported")
            if surface in {"context_numeric_limit", "token_numeric_limit"}:
                value = target[field]
                if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                    raise ValueError("numeric override surfaces require finite numeric values")
        capability = payload["capability_manifest"]
        if not isinstance(capability, dict):
            raise TypeError("governed candidate capability manifest must be an object")
        manifest = CapabilityManifest(
            tuple(capability.get("imports", ())),
            tuple(capability.get("dependencies", ())),
            tuple(capability.get("filesystem_read_roots", ())),
            tuple(capability.get("filesystem_write_roots", ())),
            tuple(capability.get("callable_tools", ())),
            capability.get("input_schema", {}),
            capability.get("output_schema", {}),
            capability.get("resource_limits", {}),
            bool(capability.get("subprocess_required", False)),
            bool(capability.get("network_required", False)),
        )
        risk_input = RiskInput(kind, tuple(sorted(set(surfaces))), tuple(operation_names), manifest)
    except (AttributeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _promotion_error("Governed candidate artifact is invalid", cause=exc)
    return ok((risk_input, source_candidate_ref))


def _resolve_materialized_surface_field(path: str, materialized: object) -> tuple[str, str] | None:
    if not isinstance(materialized, dict):
        return None
    matches: list[tuple[str, str]] = []
    for surface in materialized:
        if not isinstance(surface, str):
            continue
        prefix = f"{surface}."
        if not path.startswith(prefix):
            continue
        field = path[len(prefix) :]
        if field and "." not in field:
            matches.append((surface, field))
    if len(matches) != 1:
        return None
    return matches[0]


def _verify_recommendation(artifacts, ref: ArtifactRef, campaign_id: str, candidate_id: str, actor: ActorAssertion) -> Result:
    read = artifacts.read_bytes(ref, expected_schema="loom.promotion-recommendation.v1")
    if not read.ok:
        return read
    try:
        payload = json.loads(read.value)
        attestation = payload["attestation"]
        core = {key: value for key, value in payload.items() if key not in {"attestation", "markdown_ref"}}
        if (
            payload.get("schema_version") != "loom.promotion-recommendation.v1"
            or payload.get("campaign_id") != campaign_id
            or payload.get("candidate_id") != candidate_id
            or payload.get("disposition") != "recommend"
            or payload.get("activates_registry") is not False
            or attestation.get("subject") != actor.subject
            or attestation.get("assertion_signature") != actor.signature
            or attestation.get("payload_digest") != canonical_digest(core)
        ):
            raise ValueError("promotion recommendation identity or attestation does not match")
    except (AttributeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _promotion_error("Promotion recommendation is invalid", cause=exc)
    return ok(core)


def _recommendation_candidate_ref(payload: dict) -> Result:
    try:
        ref = ArtifactRef(**payload["candidate_artifact_ref"])
    except (KeyError, TypeError, ValueError) as exc:
        return _promotion_error("Promotion recommendation candidate binding is invalid", cause=exc)
    return ok(ref)


def _load_gate_evidence(
    artifacts,
    raw: bytes,
    identity_provider,
    campaign_id: str,
    candidate_id: str,
    candidate_evidence_ref: ArtifactRef,
    recommendation_ref: ArtifactRef,
) -> Result:
    try:
        payload = json.loads(raw)
        if payload.get("schema_version") != "loom.gate-evidence.v1":
            raise ValueError("gate evidence schema does not match")
        claims = payload["claims"]
        attestation = payload["attestation"]
        actor = ActorAssertion(**attestation["actor"])
        authenticated = identity_provider.authorize(
            actor,
            "campaign_finalizer",
            forbidden_roles=("governance_automation", "registry_operator", "governance_approver", "candidate_author"),
        )
        if not authenticated.ok:
            return authenticated
        if attestation["claims_digest"] != canonical_digest(claims):
            raise ValueError("gate evidence attestation digest does not match")
        if (
            claims.get("campaign_id") != campaign_id
            or claims.get("candidate_id") != candidate_id
            or ArtifactRef(**claims["candidate_evidence_ref"]) != candidate_evidence_ref
            or ArtifactRef(**claims["recommendation_ref"]) != recommendation_ref
        ):
            raise ValueError("gate evidence is bound to another candidate or recommendation")
        evidence_claims = claims["evidence"]
        refs = evidence_claims.get("evidence_refs")
        if not isinstance(refs, list):
            raise TypeError("gate evidence refs must be a list")
        evidence_claims["evidence_refs"] = tuple(ArtifactRef(**item) for item in refs)
        evidence = GateEvidence(**evidence_claims)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _promotion_error("Promotion gate evidence is invalid", cause=exc)
    source_checks = _load_controller_gate_sources(
        artifacts,
        evidence.evidence_refs,
        campaign_id,
        candidate_id,
        identity_provider,
    )
    return source_checks if not source_checks.ok else ok(replace(evidence, **source_checks.value))


def _load_controller_gate_sources(
    artifacts,
    refs: tuple[ArtifactRef, ...],
    campaign_id: str,
    candidate_id: str,
    identity_provider,
) -> Result:
    if not refs:
        return _promotion_error("Controller gate source evidence is required")
    merged: dict[str, bool] = {}
    for ref in refs:
        read = artifacts.read_bytes(ref, expected_schema="loom.gate-source.v1")
        if not read.ok:
            return read
        try:
            payload = json.loads(read.value)
            if payload.get("schema_version") != "loom.gate-source.v1":
                raise ValueError("gate source schema does not match")
            claims = payload["claims"]
            attestation = payload["attestation"]
            actor = ActorAssertion(**attestation["actor"])
            authorized = identity_provider.authorize(
                actor,
                "campaign_controller",
                forbidden_roles=("campaign_finalizer", "governance_automation", "registry_operator", "governance_approver", "candidate_author"),
            )
            if not authorized.ok:
                return authorized
            if (
                claims.get("campaign_id") != campaign_id
                or claims.get("candidate_id") != candidate_id
                or attestation.get("claims_digest") != canonical_digest(claims)
            ):
                raise ValueError("gate source binding or attestation does not match")
            checks = claims["checks"]
            if set(checks) != _CONTROLLER_GATE_CHECKS or any(not isinstance(value, bool) for value in checks.values()):
                raise ValueError("gate source checks are incomplete")
            for key, value in checks.items():
                if key in merged and merged[key] is not value:
                    raise ValueError("gate sources contain conflicting checks")
                merged[key] = value
            supporting_refs = tuple(ArtifactRef(**item) for item in claims["evidence_refs"])
            if not supporting_refs:
                raise ValueError("gate source has no supporting evidence")
            for supporting_ref in supporting_refs:
                verified = artifacts.read_bytes(supporting_ref)
                if not verified.ok:
                    return verified
        except (AttributeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return _promotion_error("Controller gate source evidence is invalid", cause=exc)
    if set(merged) != _CONTROLLER_GATE_CHECKS:
        return _promotion_error("Controller gate source evidence is incomplete")
    return ok(merged)


def _derive_recommendation_gates(evidence: GateEvidence, recommendation: dict, candidate_id: str) -> Result:
    try:
        holdout = recommendation["holdout_results"][candidate_id]
        if not isinstance(holdout, dict):
            raise TypeError("holdout result must be an object")
        critical_regressions = int(holdout["critical_regressions"])
        primary_improvement = float(holdout["primary_improvement_lcb"])
        if not math.isfinite(primary_improvement) or critical_regressions < 0:
            raise ValueError("holdout gate values must be finite and non-negative")
        derived = replace(
            evidence,
            holdout_passed=holdout.get("passed") is True,
            critical_regressions=critical_regressions,
            primary_improvement=primary_improvement,
            budget_correct=True,
            resource_budget_passed=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _promotion_error("Promotion recommendation lacks authoritative holdout gate evidence", cause=exc)
    return ok(derived)


def _load_governance_policy(raw: bytes) -> Result:
    try:
        payload = json.loads(raw)
        if payload.get("schema_version") != "loom.governance-policy.v1":
            raise ValueError("governance policy schema does not match")
        monitoring = payload["monitoring"]
        promotion = payload["promotion"]
        monitor_value = {
            "minimum_sample_size": int(monitoring["minimum_sample_size"]),
            "quality_regression_threshold": float(monitoring["quality_regression_threshold"]),
            "ttl_runs": int(monitoring["ttl_runs"]),
            "heartbeat_grace_seconds": int(monitoring["heartbeat_grace_seconds"]),
            "resource_ceilings": dict(monitoring.get("resource_ceilings", {})),
        }
        promotion_value = {
            "allowed_surfaces": frozenset(str(item) for item in promotion["allowed_surfaces"]),
            "auto_promote_max_risk": str(promotion["auto_promote_max_risk"]),
            "executable_requires_human": bool(promotion["executable_requires_human"]),
            "require_holdout": bool(promotion["require_holdout"]),
            "minimum_improvement": float(promotion["minimum_improvement"]),
        }
        if monitor_value["minimum_sample_size"] < 1 or monitor_value["ttl_runs"] < 1 or monitor_value["heartbeat_grace_seconds"] < 1:
            raise ValueError("monitoring policy limits must be positive")
        if (
            not math.isfinite(monitor_value["quality_regression_threshold"])
            or monitor_value["quality_regression_threshold"] < 0
            or any(
                isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0
                for value in monitor_value["resource_ceilings"].values()
            )
        ):
            raise ValueError("monitoring policy thresholds must be finite and non-negative")
        if (
            not promotion_value["allowed_surfaces"]
            or promotion_value["auto_promote_max_risk"] != "low"
            or not promotion_value["executable_requires_human"]
            or not promotion_value["require_holdout"]
            or not math.isfinite(promotion_value["minimum_improvement"])
            or promotion_value["minimum_improvement"] < 0
        ):
            raise ValueError("initial governance promotion policy must be fail-closed")
        value = {"monitoring": monitor_value, "promotion": promotion_value}
    except (AttributeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _promotion_error("Governance monitoring policy is invalid", cause=exc)
    return ok(value)


def _load_risk_rule_set(raw: bytes) -> Result:
    try:
        payload = json.loads(raw)
        if payload.get("schema_version") != "loom.risk-rule-set.v1" or payload.get("implementation_version") != "loom.default-risk-rules.v1":
            raise ValueError("risk rule set is not compatible with this deterministic classifier")
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _promotion_error("Governance risk rule set is invalid", cause=exc)
    return ok(payload)


def _promotion_error(message: str, *, cause: BaseException | None = None) -> Result:
    return err(
        make_loom_error(
            "GOVERNANCE_FAILED",
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
        )
    )


__all__ = [
    "GovernedPromotionController",
    "PromotionRequest",
    "publish_candidate_evidence",
    "publish_gate_evidence",
    "publish_gate_source_evidence",
]
