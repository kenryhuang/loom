from __future__ import annotations

from loom.campaigns.contracts import ArtifactRef, GateResult, RiskLevel
from loom.governance.gates import GateEvidence, evaluate_gates, promotion_disposition


def _ref(kind: str, digest: str) -> ArtifactRef:
    return ArtifactRef(f"{kind}.v1", kind, f"{kind}.json", digest, 1)


def _evidence(**overrides) -> GateEvidence:
    values = {
        "artifact_integrity": True,
        "evidence_integrity": True,
        "task_set_isolation": True,
        "contamination_free": True,
        "evaluator_independent": True,
        "sandbox_conformant": True,
        "forbidden_surface_free": True,
        "security_regression_passed": True,
        "budget_correct": True,
        "baseline_fresh": True,
        "policy_fresh": True,
        "rollback_verified": True,
        "monitoring_ready": True,
        "holdout_passed": True,
        "critical_regressions": 0,
        "primary_improvement": 0.05,
        "required_improvement": 0.01,
        "resource_budget_passed": True,
        "evidence_refs": (_ref("evidence", "b" * 64),),
        **overrides,
    }
    return GateEvidence(**values)


def test_low_risk_declarative_candidate_passes_only_with_all_mandatory_gates():
    gates = evaluate_gates(_evidence(), RiskLevel.LOW, executable=False)

    assert all(gate.result is GateResult.PASSED for gate in gates)
    assert promotion_disposition(gates, RiskLevel.LOW, executable=False, approval=None).value == "promoted"


def test_mandatory_failure_cannot_be_overridden_by_human_approval():
    gates = evaluate_gates(_evidence(task_set_isolation=False), RiskLevel.MEDIUM, executable=False)

    assert any(gate.gate_id == "task_set_isolation" and gate.mandatory and gate.result is GateResult.FAILED for gate in gates)
    assert promotion_disposition(gates, RiskLevel.MEDIUM, executable=False, approval=object()).value == "rejected"


def test_medium_and_executable_candidates_require_review_but_high_risk_declarative_is_rejected():
    medium = evaluate_gates(_evidence(), RiskLevel.MEDIUM, executable=False)
    executable = evaluate_gates(_evidence(), RiskLevel.HIGH, executable=True)
    high = evaluate_gates(_evidence(), RiskLevel.HIGH, executable=False)

    assert promotion_disposition(medium, RiskLevel.MEDIUM, executable=False, approval=None).value == "awaiting_approval"
    assert promotion_disposition(executable, RiskLevel.HIGH, executable=True, approval=None).value == "awaiting_approval"
    assert promotion_disposition(executable, RiskLevel.HIGH, executable=True, approval=object()).value == "promoted"
    assert promotion_disposition(high, RiskLevel.HIGH, executable=False, approval=object()).value == "rejected"


def test_missing_evidence_fails_closed():
    gates = evaluate_gates(_evidence(evidence_refs=()), RiskLevel.LOW, executable=False)

    evidence_gate = next(gate for gate in gates if gate.gate_id == "evidence_integrity")
    assert evidence_gate.result is GateResult.INSUFFICIENT_EVIDENCE
    assert promotion_disposition(gates, RiskLevel.LOW, executable=False, approval=None).value == "rejected"
