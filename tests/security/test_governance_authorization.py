from __future__ import annotations

import asyncio
from pathlib import Path

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.contracts import ApprovalRecord, ArtifactRef
from loom.campaigns.serialization import new_prefixed_id
from loom.governance.policy import ActorAssertion, GovernanceOperation, GovernanceReview, StaticIdentityProvider
from loom.governance.registry import SQLiteGovernanceStore
from tests.governance.conftest import fixture_artifacts


def _assertion(subject, roles, *, signature="signed"):
    return ActorAssertion(subject, roles, "2026-07-18T00:00:00.000000Z", "2999-01-01T00:00:00.000000Z", signature)


def _approval(subject: str) -> ApprovalRecord:
    return ApprovalRecord(
        subject,
        "governance_approver",
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "c" * 64,
        "b" * 64,
        "p" * 64,
        "r" * 64,
        "g" * 64,
        "approve",
    )


def test_review_enforces_role_and_finalizer_separation_and_revocation(tmp_path: Path):
    async def scenario():
        approver = _assertion("alice", ("governance_approver",))
        finalizer = _assertion("bob", ("governance_approver", "campaign_finalizer"))
        revoked = _assertion("carol", ("governance_approver",), signature="revoked")
        identity = StaticIdentityProvider((approver, finalizer, revoked), revoked_signatures=("revoked",))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        review = GovernanceReview(new_prefixed_id("review_"), "cand_a", approver, _approval("alice"))

        accepted = await store.record_review(review, "g" * 64)
        conflict = await store.record_review(
            GovernanceReview(new_prefixed_id("review_"), "cand_b", finalizer, _approval("bob")),
            "g" * 64,
        )
        denied = await store.record_review(
            GovernanceReview(new_prefixed_id("review_"), "cand_c", revoked, _approval("carol")),
            "g" * 64,
        )

        assert accepted.ok
        assert not conflict.ok and conflict.error.code == "AUTHORIZATION_FAILED"
        assert not denied.ok and denied.error.code == "AUTHENTICATION_FAILED"

    asyncio.run(scenario())


def test_registry_operation_requires_registry_operator_role(tmp_path: Path):
    async def scenario():
        actor = _assertion("alice", ("governance_approver",))
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((actor,)), fixture_artifacts())

        result = await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "surface", actor, "activate"), 0)

        assert not result.ok
        assert result.error.code == "AUTHORIZATION_FAILED"

    asyncio.run(scenario())


def test_registry_rejects_nonexistent_initial_artifact(tmp_path: Path):
    async def scenario():
        actor = _assertion("admin", ("governance_admin",))
        artifacts = ArtifactStore(tmp_path / "artifacts")
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((actor,)), artifacts)
        missing = ArtifactRef("candidate.v1", "candidate", "objects/ff/missing.json", "f" * 64, 1)

        result = await store.initialize_surface("context_policy", missing, actor)

        assert not result.ok
        assert result.error.code == "ARTIFACT_NOT_FOUND"

    asyncio.run(scenario())
