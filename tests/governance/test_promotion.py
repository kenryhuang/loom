from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.contracts import PromotionDisposition
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes
from loom.governance.gates import GateEvidence
from loom.governance.policy import ActorAssertion, StaticIdentityProvider
from loom.governance.promotion import (
    GovernedPromotionController,
    PromotionRequest,
    publish_candidate_evidence,
    publish_gate_evidence,
    publish_gate_source_evidence,
)
from loom.governance.registry import MonitorRegistration, SQLiteGovernanceStore

from .conftest import artifact


def _actor():
    return ActorAssertion(
        "automation",
        ("governance_automation",),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "signed",
    )


def _finalizer():
    return ActorAssertion(
        "finalizer",
        ("campaign_finalizer",),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "finalizer-signed",
    )


def _admin():
    return ActorAssertion(
        "governance-admin",
        ("governance_admin",),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "admin-signed",
    )


def _controller():
    return ActorAssertion(
        "controller",
        ("campaign_controller",),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "controller-signed",
    )


def _raw_evidence(evidence_ref) -> GateEvidence:
    return GateEvidence(
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
        0.05,
        0.01,
        True,
        (evidence_ref,),
    )


def _evidence(artifacts, candidate_id: str, supporting_ref, identities) -> GateEvidence:
    source = publish_gate_source_evidence(
        artifacts,
        "cmp_test",
        candidate_id,
        {
            "task_set_isolation": True,
            "contamination_free": True,
            "evaluator_independent": True,
            "sandbox_conformant": True,
            "security_regression_passed": True,
        },
        (supporting_ref,),
        _controller(),
        identities,
    ).unwrap()
    return _raw_evidence(source)


def _published(artifacts: ArtifactStore, kind: str, content: bytes):
    return artifacts.publish_bytes(content, kind=kind, schema_version=f"{kind}.v1", suffix=".json").unwrap()


def _policy(artifacts: ArtifactStore, *, minimum_improvement: float = 0.01):
    return artifacts.publish_bytes(
        canonical_json_bytes(
            {
                "schema_version": "loom.governance-policy.v1",
                "promotion": {
                    "allowed_surfaces": ["context_numeric_limit", "system_prompt"],
                    "auto_promote_max_risk": "low",
                    "executable_requires_human": True,
                    "require_holdout": True,
                    "minimum_improvement": minimum_improvement,
                },
                "monitoring": {
                    "minimum_sample_size": 3,
                    "quality_regression_threshold": 0.05,
                    "ttl_runs": 10,
                    "heartbeat_grace_seconds": 60,
                },
            }
        ),
        kind="policy",
        schema_version="loom.governance-policy.v1",
    ).unwrap()


def _risk_rules(artifacts: ArtifactStore):
    return artifacts.publish_bytes(
        canonical_json_bytes(
            {
                "schema_version": "loom.risk-rule-set.v1",
                "implementation_version": "loom.default-risk-rules.v1",
            }
        ),
        kind="risk_rules",
        schema_version="loom.risk-rule-set.v1",
    ).unwrap()


async def _authorize_governance(store, policy, rules, admin):
    assert (await store.authorize_governance_artifact(policy, "policy", admin)).ok
    assert (await store.authorize_governance_artifact(rules, "risk_rules", admin)).ok


def _monitor(policy, monitor_id: str, surface: str):
    return MonitorRegistration(
        monitor_id,
        surface,
        "pending",
        True,
        0,
        3,
        0.05,
        ttl_runs=10,
        heartbeat_grace_seconds=60,
        policy_digest=policy.sha256,
    )


def _source_candidate(artifacts, candidate_id: str):
    return artifacts.publish_bytes(
        canonical_json_bytes(
            {
                "schema_version": "loom.candidate-bundle.v1",
                "campaign_id": "cmp_test",
                "candidate_id": candidate_id,
                "kind": "declarative_patch",
            }
        ),
        kind="candidate_bundle",
        schema_version="loom.candidate-bundle.v1",
    ).unwrap()


def _recommendation(artifacts, candidate_id: str, finalizer):
    core = {
        "schema_version": "loom.promotion-recommendation.v1",
        "campaign_id": "cmp_test",
        "candidate_id": candidate_id,
        "candidate_artifact_ref": _source_candidate(artifacts, candidate_id),
        "holdout_results": {
            candidate_id: {
                "passed": True,
                "critical_regressions": 0,
                "primary_improvement_lcb": 0.05,
            }
        },
        "disposition": "recommend",
        "activates_registry": False,
    }
    payload = {
        **core,
        "attestation": {
            "subject": finalizer.subject,
            "roles": finalizer.roles,
            "assertion_signature": finalizer.signature,
            "payload_digest": canonical_digest(core),
        },
    }
    return artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="promotion_recommendation",
        schema_version="loom.promotion-recommendation.v1",
    ).unwrap()


def _governed_candidate(artifacts, candidate_id: str, surface: str, operation: str, *, source_candidate_id: str | None = None):
    return artifacts.publish_bytes(
        canonical_json_bytes(
            {
                "schema_version": "loom.governed-candidate.v1",
                "campaign_id": "cmp_test",
                "candidate_id": candidate_id,
                "source_candidate_ref": _source_candidate(artifacts, source_candidate_id or candidate_id),
                "kind": "declarative_patch",
                "operations": [{"op": operation, "path": f"{surface}.value", "value": 128}],
                "capability_manifest": {
                    "imports": [],
                    "dependencies": [],
                    "filesystem_read_roots": [],
                    "filesystem_write_roots": [],
                    "callable_tools": [],
                    "input_schema": {},
                    "output_schema": {},
                    "resource_limits": {},
                },
                "materialized": {surface: {"value": 128}},
            }
        ),
        kind="governed_candidate",
        schema_version="loom.governed-candidate.v1",
    ).unwrap()


def test_low_risk_declarative_promotion_activates_only_through_atomic_store(tmp_path: Path):
    async def scenario():
        actor = _actor()
        finalizer = _finalizer()
        admin = _admin()
        artifacts = ArtifactStore(tmp_path / "artifacts")
        identities = StaticIdentityProvider((actor, finalizer, admin, _controller()))
        store = SQLiteGovernanceStore(tmp_path / "governance", identities, artifacts)
        baseline = _published(artifacts, "baseline", b"baseline")
        policy = _policy(artifacts)
        rules = _risk_rules(artifacts)
        evidence_ref = _published(artifacts, "evidence", b"evidence")
        active_candidate = _governed_candidate(artifacts, "cand_a", "context_numeric_limit", "set_limit")
        candidate = publish_candidate_evidence(
            artifacts,
            "cmp_test",
            "cand_a",
            active_candidate,
            _recommendation(artifacts, "cand_a", finalizer),
            finalizer,
            identities,
        ).unwrap()
        gate_evidence = publish_gate_evidence(
            artifacts,
            _evidence(artifacts, "cand_a", evidence_ref, identities),
            "cmp_test",
            "cand_a",
            candidate,
            finalizer,
            identities,
        ).unwrap()
        await _authorize_governance(store, policy, rules, admin)
        await store.initialize_surface("context_numeric_limit", baseline, admin)
        controller = GovernedPromotionController(store, artifacts, rules)
        request = PromotionRequest(
            "cmp_test",
            "cand_a",
            "context_numeric_limit",
            candidate,
            baseline,
            baseline.sha256,
            policy,
            gate_evidence,
            None,
            "op_promote_low",
            "2026-07-18T00:00:00.000000Z",
        )

        result = await controller.promote(
            request,
            actor,
            _monitor(policy, "monitor_a", "context_numeric_limit"),
        )

        assert result.ok
        assert result.value.decision is PromotionDisposition.PROMOTED
        assert (await store.active("context_numeric_limit")).unwrap().artifact == active_candidate
        replay = await controller.promote(request, actor, _monitor(policy, "monitor_a", "context_numeric_limit"))
        assert replay.ok and replay.value == result.value
        changed_replay = await controller.promote(
            request,
            actor,
            _monitor(policy, "different_monitor", "context_numeric_limit"),
        )
        assert not changed_replay.ok and changed_replay.error.code == "OPERATION_CONFLICT"
        assert (await store.audit("context_numeric_limit")).unwrap()["decisions"] == 1

    asyncio.run(scenario())


def test_medium_risk_without_approval_remains_awaiting_and_does_not_change_pointer(tmp_path: Path):
    async def scenario():
        actor = _actor()
        finalizer = _finalizer()
        admin = _admin()
        artifacts = ArtifactStore(tmp_path / "artifacts")
        identities = StaticIdentityProvider((actor, finalizer, admin, _controller()))
        store = SQLiteGovernanceStore(tmp_path / "governance", identities, artifacts)
        baseline = _published(artifacts, "baseline", b"baseline")
        policy = _policy(artifacts)
        rules = _risk_rules(artifacts)
        evidence_ref = _published(artifacts, "evidence", b"evidence")
        candidate = publish_candidate_evidence(
            artifacts,
            "cmp_test",
            "cand_prompt",
            _governed_candidate(artifacts, "cand_prompt", "system_prompt", "replace"),
            _recommendation(artifacts, "cand_prompt", finalizer),
            finalizer,
            identities,
        ).unwrap()
        gate_evidence = publish_gate_evidence(
            artifacts,
            _evidence(artifacts, "cand_prompt", evidence_ref, identities),
            "cmp_test",
            "cand_prompt",
            candidate,
            finalizer,
            identities,
        ).unwrap()
        await _authorize_governance(store, policy, rules, admin)
        await store.initialize_surface("system_prompt", baseline, admin)
        controller = GovernedPromotionController(store, artifacts, rules)
        request = PromotionRequest(
            "cmp_test",
            "cand_prompt",
            "system_prompt",
            candidate,
            baseline,
            baseline.sha256,
            policy,
            gate_evidence,
            None,
            "op_promote_medium",
            "2026-07-18T00:00:00.000000Z",
        )

        result = await controller.promote(
            request,
            actor,
            _monitor(policy, "monitor_prompt", "system_prompt"),
        )

        assert result.ok and result.value.decision is PromotionDisposition.AWAITING_APPROVAL
        assert (await store.active("system_prompt")).unwrap().artifact == baseline
        replay = await controller.promote(request, actor, _monitor(policy, "monitor_prompt", "system_prompt"))
        assert replay.ok and replay.value == result.value
        changed_replay = await controller.promote(request, actor, _monitor(policy, "changed_monitor", "system_prompt"))
        assert not changed_replay.ok and changed_replay.error.code == "OPERATION_CONFLICT"
        assert (await store.audit("system_prompt")).unwrap()["decisions"] == 1

    asyncio.run(scenario())


def test_promotion_rejects_a_policy_that_was_not_authorized_by_the_governance_admin(tmp_path: Path):
    async def scenario():
        actor, finalizer, admin = _actor(), _finalizer(), _admin()
        artifacts = ArtifactStore(tmp_path / "artifacts")
        identities = StaticIdentityProvider((actor, finalizer, admin, _controller()))
        store = SQLiteGovernanceStore(tmp_path / "governance", identities, artifacts)
        baseline = _published(artifacts, "baseline", b"baseline")
        policy = _policy(artifacts)
        rules = _risk_rules(artifacts)
        evidence_ref = _published(artifacts, "evidence", b"evidence")
        active_candidate = _governed_candidate(artifacts, "cand_unapproved_policy", "context_numeric_limit", "set_limit")
        candidate = publish_candidate_evidence(
            artifacts,
            "cmp_test",
            "cand_unapproved_policy",
            active_candidate,
            _recommendation(artifacts, "cand_unapproved_policy", finalizer),
            finalizer,
            identities,
        ).unwrap()
        gate_evidence = publish_gate_evidence(
            artifacts,
            _evidence(artifacts, "cand_unapproved_policy", evidence_ref, identities),
            "cmp_test",
            "cand_unapproved_policy",
            candidate,
            finalizer,
            identities,
        ).unwrap()
        assert (await store.authorize_governance_artifact(rules, "risk_rules", admin)).ok
        assert (await store.initialize_surface("context_numeric_limit", baseline, admin)).ok

        result = await GovernedPromotionController(store, artifacts, rules).promote(
            PromotionRequest(
                "cmp_test",
                "cand_unapproved_policy",
                "context_numeric_limit",
                candidate,
                baseline,
                baseline.sha256,
                policy,
                gate_evidence,
                None,
                "op_unapproved_policy",
                "2026-07-18T00:00:00.000000Z",
            ),
            actor,
            _monitor(policy, "monitor_unapproved_policy", "context_numeric_limit"),
        )

        assert not result.ok and result.error.code == "AUTHORIZATION_FAILED"
        assert (await store.active("context_numeric_limit")).unwrap().artifact == baseline

    asyncio.run(scenario())


def test_promotion_fails_closed_when_any_authoritative_artifact_is_missing(tmp_path: Path):
    async def scenario():
        actor = _actor()
        finalizer = _finalizer()
        admin = _admin()
        artifacts = ArtifactStore(tmp_path / "artifacts")
        identities = StaticIdentityProvider((actor, finalizer, admin, _controller()))
        store = SQLiteGovernanceStore(tmp_path / "governance", identities, artifacts)
        baseline = _published(artifacts, "baseline", b"baseline")
        policy = _policy(artifacts)
        rules = _risk_rules(artifacts)
        evidence_ref = _published(artifacts, "evidence", b"evidence")
        valid_candidate = publish_candidate_evidence(
            artifacts,
            "cmp_test",
            "cand_missing",
            _governed_candidate(artifacts, "cand_missing", "context_numeric_limit", "set_limit"),
            _recommendation(artifacts, "cand_missing", finalizer),
            finalizer,
            identities,
        ).unwrap()
        gate_evidence = publish_gate_evidence(
            artifacts,
            _evidence(artifacts, "cand_missing", evidence_ref, identities),
            "cmp_test",
            "cand_missing",
            valid_candidate,
            finalizer,
            identities,
        ).unwrap()
        missing = artifact("promotion_candidate", "f")
        await _authorize_governance(store, policy, rules, admin)
        await store.initialize_surface("context_numeric_limit", baseline, admin)
        controller = GovernedPromotionController(store, artifacts, rules)

        result = await controller.promote(
            PromotionRequest(
                "cmp_test",
                "cand_missing",
                "context_numeric_limit",
                missing,
                baseline,
                baseline.sha256,
                policy,
                gate_evidence,
                None,
                "op_promote_missing",
                "2026-07-18T00:00:00.000000Z",
            ),
            actor,
            _monitor(policy, "monitor", "context_numeric_limit"),
        )

        assert not result.ok and result.error.code in {"ARTIFACT_NOT_FOUND", "ARTIFACT_INTEGRITY_FAILED"}
        assert (await store.active("context_numeric_limit")).unwrap().version == 0

    asyncio.run(scenario())


def test_gate_evidence_requires_a_trusted_separate_finalizer(tmp_path: Path):
    actor = _actor()
    finalizer = _finalizer()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    identities = StaticIdentityProvider((actor, finalizer))
    evidence_ref = _published(artifacts, "evidence", b"evidence")

    denied = publish_gate_evidence(
        artifacts,
        _raw_evidence(evidence_ref),
        "cmp_test",
        "cand_test",
        evidence_ref,
        actor,
        identities,
    )

    assert not denied.ok and denied.error.code == "AUTHORIZATION_FAILED"


def test_candidate_risk_is_derived_from_exact_materialized_override(tmp_path: Path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    finalizer = _finalizer()
    identities = StaticIdentityProvider((finalizer,))
    candidate = artifacts.publish_bytes(
        canonical_json_bytes(
            {
                "schema_version": "loom.governed-candidate.v1",
                "campaign_id": "cmp_test",
                "candidate_id": "cand_hidden_surface",
                "source_candidate_ref": _source_candidate(artifacts, "cand_hidden_surface"),
                "kind": "declarative_patch",
                "operations": [{"op": "set_limit", "path": "context_numeric_limit.value", "value": 128}],
                "capability_manifest": {
                    "imports": [],
                    "dependencies": [],
                    "filesystem_read_roots": [],
                    "filesystem_write_roots": [],
                    "callable_tools": [],
                    "input_schema": {},
                    "output_schema": {},
                    "resource_limits": {},
                },
                "materialized": {
                    "context_numeric_limit": {"value": 128},
                    "governance": {"mandatory_gates": False},
                },
            }
        ),
        kind="governed_candidate",
        schema_version="loom.governed-candidate.v1",
    ).unwrap()

    result = publish_candidate_evidence(
        artifacts,
        "cmp_test",
        "cand_hidden_surface",
        candidate,
        _recommendation(artifacts, "cand_hidden_surface", finalizer),
        finalizer,
        identities,
    )

    assert not result.ok and result.error.code == "GOVERNANCE_FAILED"


def test_candidate_evidence_rejects_a_post_holdout_candidate_substitution(tmp_path: Path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    finalizer = _finalizer()
    identities = StaticIdentityProvider((finalizer,))
    substituted = _governed_candidate(
        artifacts,
        "cand_bound",
        "context_numeric_limit",
        "set_limit",
        source_candidate_id="cand_unevaluated",
    )

    result = publish_candidate_evidence(
        artifacts,
        "cmp_test",
        "cand_bound",
        substituted,
        _recommendation(artifacts, "cand_bound", finalizer),
        finalizer,
        identities,
    )

    assert not result.ok and result.error.code == "GOVERNANCE_FAILED"


def test_frozen_policy_overrides_candidate_claimed_improvement_threshold(tmp_path: Path):
    async def scenario():
        actor = _actor()
        finalizer = _finalizer()
        admin = _admin()
        artifacts = ArtifactStore(tmp_path / "artifacts")
        identities = StaticIdentityProvider((actor, finalizer, admin, _controller()))
        store = SQLiteGovernanceStore(tmp_path / "governance", identities, artifacts)
        baseline = _published(artifacts, "baseline", b"baseline")
        policy = _policy(artifacts, minimum_improvement=0.10)
        rules = _risk_rules(artifacts)
        evidence_ref = _published(artifacts, "evidence", b"evidence")
        candidate = publish_candidate_evidence(
            artifacts,
            "cmp_test",
            "cand_threshold",
            _governed_candidate(artifacts, "cand_threshold", "context_numeric_limit", "set_limit"),
            _recommendation(artifacts, "cand_threshold", finalizer),
            finalizer,
            identities,
        ).unwrap()
        claimed = replace(
            _evidence(artifacts, "cand_threshold", evidence_ref, identities),
            holdout_passed=False,
            critical_regressions=99,
            primary_improvement=999.0,
            budget_correct=False,
            resource_budget_passed=False,
        )
        gate_evidence = publish_gate_evidence(
            artifacts,
            claimed,
            "cmp_test",
            "cand_threshold",
            candidate,
            finalizer,
            identities,
        ).unwrap()
        persisted_claims = json.loads(artifacts.read_bytes(gate_evidence).unwrap())["claims"]["evidence"]
        assert persisted_claims["holdout_passed"] is True
        assert persisted_claims["critical_regressions"] == 0
        assert persisted_claims["primary_improvement"] == 0.05
        assert persisted_claims["budget_correct"] is True
        assert persisted_claims["resource_budget_passed"] is True
        await _authorize_governance(store, policy, rules, admin)
        await store.initialize_surface("context_numeric_limit", baseline, admin)

        result = await GovernedPromotionController(store, artifacts, rules).promote(
            PromotionRequest(
                "cmp_test",
                "cand_threshold",
                "context_numeric_limit",
                candidate,
                baseline,
                baseline.sha256,
                policy,
                gate_evidence,
                None,
                "op_threshold",
                "2026-07-18T00:00:00.000000Z",
            ),
            actor,
            _monitor(policy, "monitor_threshold", "context_numeric_limit"),
        )

        assert result.ok and result.value.decision is PromotionDisposition.REJECTED
        assert (await store.active("context_numeric_limit")).unwrap().artifact == baseline
        assert (await store.audit("context_numeric_limit")).unwrap()["decisions"] == 1

    asyncio.run(scenario())
