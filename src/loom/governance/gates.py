"""Deterministic mandatory and review promotion gates."""

from __future__ import annotations

import math
from dataclasses import dataclass

from loom.campaigns.contracts import ArtifactRef, GateDecision, GateResult, PromotionDisposition, RiskLevel


@dataclass(frozen=True, slots=True)
class GateEvidence:
    artifact_integrity: bool
    evidence_integrity: bool
    task_set_isolation: bool
    contamination_free: bool
    evaluator_independent: bool
    sandbox_conformant: bool
    forbidden_surface_free: bool
    security_regression_passed: bool
    budget_correct: bool
    baseline_fresh: bool
    policy_fresh: bool
    rollback_verified: bool
    monitoring_ready: bool
    holdout_passed: bool
    critical_regressions: int
    primary_improvement: float | None
    required_improvement: float
    resource_budget_passed: bool
    evidence_refs: tuple[ArtifactRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        numeric = (self.primary_improvement, self.required_improvement)
        if any(value is not None and (isinstance(value, bool) or not math.isfinite(float(value))) for value in numeric):
            raise ValueError("Gate evidence metrics must be finite")
        if isinstance(self.critical_regressions, bool) or self.critical_regressions < 0:
            raise ValueError("Gate evidence critical regressions must be non-negative")


def evaluate_gates(evidence: GateEvidence, risk: RiskLevel, *, executable: bool) -> tuple[GateDecision, ...]:
    checks = (
        ("artifact_integrity", evidence.artifact_integrity),
        ("evidence_integrity", evidence.evidence_integrity),
        ("task_set_isolation", evidence.task_set_isolation),
        ("contamination_free", evidence.contamination_free),
        ("evaluator_independent", evidence.evaluator_independent),
        ("sandbox_conformance", evidence.sandbox_conformant),
        ("forbidden_surface", evidence.forbidden_surface_free),
        ("security_regression", evidence.security_regression_passed),
        ("budget_correctness", evidence.budget_correct),
        ("baseline_freshness", evidence.baseline_fresh),
        ("policy_freshness", evidence.policy_fresh),
        ("rollback_verification", evidence.rollback_verified),
        ("monitoring_readiness", evidence.monitoring_ready),
        ("holdout", evidence.holdout_passed),
        ("critical_regressions", evidence.critical_regressions == 0),
        ("resource_budget", evidence.resource_budget_passed),
        (
            "primary_improvement",
            evidence.primary_improvement is not None and evidence.primary_improvement >= evidence.required_improvement,
        ),
    )
    decisions: list[GateDecision] = []
    for gate_id, passed in checks:
        if gate_id == "evidence_integrity" and not evidence.evidence_refs:
            result = GateResult.INSUFFICIENT_EVIDENCE
        else:
            result = GateResult.PASSED if passed else GateResult.FAILED
        decisions.append(GateDecision(gate_id, True, result, evidence.evidence_refs, "passed" if passed else "failed closed"))
    if risk in {RiskLevel.MEDIUM, RiskLevel.HIGH} or executable:
        decisions.append(GateDecision("human_review", False, GateResult.REVIEW_REQUIRED, evidence.evidence_refs, "human approval required"))
    if risk is RiskLevel.FORBIDDEN:
        decisions.append(GateDecision("risk", True, GateResult.FAILED, evidence.evidence_refs, "forbidden risk"))
    return tuple(decisions)


def promotion_disposition(gates: tuple[GateDecision, ...], risk: RiskLevel, *, executable: bool, approval) -> PromotionDisposition:
    if any(gate.mandatory and gate.result is not GateResult.PASSED for gate in gates):
        return PromotionDisposition.REJECTED
    if risk is RiskLevel.FORBIDDEN or (risk is RiskLevel.HIGH and not executable):
        return PromotionDisposition.REJECTED
    if risk in {RiskLevel.MEDIUM, RiskLevel.HIGH} or executable:
        return PromotionDisposition.PROMOTED if approval is not None else PromotionDisposition.AWAITING_APPROVAL
    return PromotionDisposition.PROMOTED


__all__ = ["GateEvidence", "evaluate_gates", "promotion_disposition"]
