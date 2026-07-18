from __future__ import annotations

from loom.campaigns.contracts import (
    ApprovalRecord,
    ArtifactRef,
    GateDecision,
    GateResult,
    PromotionDecision,
    PromotionDisposition,
    RiskLevel,
)
from loom.campaigns.serialization import canonical_digest, new_prefixed_id, utc_now
from loom.core import ok
from loom.governance.policy import ActorAssertion


def governance_admin() -> ActorAssertion:
    return ActorAssertion(
        "governance-admin",
        ("governance_admin",),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "signed-governance-admin",
    )


class FixtureArtifactResolver:
    """Registers deterministic fixture refs without weakening production code."""

    def read_bytes(self, ref):
        return ok((ref.kind + ":" + ref.sha256).encode())


def fixture_artifacts():
    return FixtureArtifactResolver()


def artifact(kind: str, char: str) -> ArtifactRef:
    return ArtifactRef(f"{kind}.v1", kind, f"{kind}-{char}.json", char * 64, 1)


def approval(
    subject: str = "approver",
    *,
    candidate_digest: str = "c" * 64,
    policy_digest: str = "d" * 64,
    risk_rules_digest: str = "a" * 64,
    gate_digest: str = "e" * 64,
    baseline_digest: str = "b" * 64,
) -> ApprovalRecord:
    return ApprovalRecord(
        subject,
        "governance_approver",
        utc_now(),
        "2999-01-01T00:00:00.000000Z",
        candidate_digest,
        baseline_digest,
        policy_digest,
        risk_rules_digest,
        gate_digest,
        "approve",
    )


def decision(candidate: str = "cand_a", *, approved=False, active_char="c", rollback_char="b") -> PromotionDecision:
    gates = (GateDecision("integrity", True, GateResult.PASSED, (artifact("evidence", "e"),), "passed"),)
    return PromotionDecision(
        "loom.promotion-decision.v1",
        new_prefixed_id("decision_"),
        "cmp_test",
        candidate,
        utc_now(),
        rollback_char * 64,
        "d" * 64,
        artifact("risk_rules", "a"),
        gates,
        RiskLevel.MEDIUM if approved else RiskLevel.LOW,
        ("prompt_instruction",) if approved else ("context_numeric_limit",),
        approval(
            candidate_digest=active_char * 64,
            baseline_digest=rollback_char * 64,
            gate_digest=canonical_digest(gates),
        )
        if approved
        else None,
        PromotionDisposition.PROMOTED,
        artifact("candidate", active_char),
        artifact("rollback", rollback_char),
    )
