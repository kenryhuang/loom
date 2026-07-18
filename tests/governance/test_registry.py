from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from loom.campaigns.contracts import RiskLevel
from loom.campaigns.serialization import canonical_digest, new_prefixed_id
from loom.governance.policy import ActorAssertion, GovernanceOperation, GovernanceReview, StaticIdentityProvider
from loom.governance.registry import MonitorRegistration, SQLiteGovernanceStore

from .conftest import approval, artifact, decision, fixture_artifacts, governance_admin


def _actor(subject="operator", roles=("registry_operator",)):
    return ActorAssertion(subject, roles, "2026-07-18T00:00:00.000000Z", "2999-01-01T00:00:00.000000Z", "signed")


def test_registry_bootstrap_and_authority_artifacts_require_an_independent_admin(tmp_path: Path):
    async def scenario():
        operator = _actor()
        admin = governance_admin()
        store = SQLiteGovernanceStore(
            tmp_path / "governance",
            StaticIdentityProvider((operator, admin)),
            fixture_artifacts(),
        )
        baseline = artifact("baseline", "b")
        policy = artifact("policy", "d")

        missing = await store.initialize_surface("completion_policy", baseline)
        wrong_role = await store.initialize_surface("completion_policy", baseline, operator)
        unauthorized_policy = await store.authorize_governance_artifact(policy, "policy", operator)

        assert not missing.ok and missing.error.code == "AUTHENTICATION_FAILED"
        assert not wrong_role.ok and wrong_role.error.code == "AUTHORIZATION_FAILED"
        assert not unauthorized_policy.ok and unauthorized_policy.error.code == "AUTHORIZATION_FAILED"
        assert (await store.initialize_surface("completion_policy", baseline, admin)).ok
        assert (await store.authorize_governance_artifact(policy, "policy", admin)).ok
        assert (await store.require_authorized_governance_artifact(policy, "policy")).ok

    asyncio.run(scenario())


def test_activation_commits_decision_pointer_monitor_and_event_atomically(tmp_path: Path):
    async def scenario():
        admin = governance_admin()
        identity = StaticIdentityProvider((_actor(), admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        baseline = artifact("baseline", "b")
        await store.initialize_surface("completion_policy", baseline, admin)
        lease = (
            await store.acquire_lease(
                GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor(), "activate"),
                expected_active_version=0,
            )
        ).unwrap()
        monitor = MonitorRegistration("monitor_a", "completion_policy", "pending", True, 0, 5, 0.05)

        active = await store.activate(lease, decision(), monitor)

        assert active.ok
        assert active.value.version == 1
        assert active.value.artifact.sha256 == "c" * 64
        audit = (await store.audit("completion_policy")).unwrap()
        assert audit["decisions"] == 1
        assert audit["events"] == 2  # initialized + activated
        assert audit["active_monitors"] == 1

    asyncio.run(scenario())


def test_governance_operation_and_activation_replay_return_original_result(tmp_path: Path):
    async def scenario():
        admin = governance_admin()
        identity = StaticIdentityProvider((_actor(), admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        operation = GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor(), "activate")
        first_lease = (await store.acquire_lease(operation, 0)).unwrap()
        replayed_lease = (await store.acquire_lease(operation, 0)).unwrap()
        assert replayed_lease == first_lease
        promoted = decision()
        monitor = MonitorRegistration("replay", "completion_policy", "pending", True, 0, 5, 0.05)

        first = await store.activate(first_lease, promoted, monitor)
        replay = await store.activate(replayed_lease, promoted, monitor)

        assert replay.ok and replay.value == first.value
        assert (await store.audit("completion_policy")).unwrap()["decisions"] == 1

    asyncio.run(scenario())


def test_activation_rolls_back_all_writes_on_failpoint_and_rejects_unhealthy_monitor(tmp_path: Path):
    async def scenario():
        admin = governance_admin()
        identity = StaticIdentityProvider((_actor(), admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts(), failpoint=lambda name: name == "before_commit")
        baseline = artifact("baseline", "b")
        await store.initialize_surface("completion_policy", baseline, admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor(), "activate"), 0)).unwrap()
        healthy = MonitorRegistration("monitor_a", "completion_policy", "pending", True, 0, 5, 0.05)

        failed = await store.activate(lease, decision(), healthy)

        assert not failed.ok
        assert (await store.active("completion_policy")).unwrap().version == 0
        audit = (await store.audit("completion_policy")).unwrap()
        assert audit["decisions"] == 0 and audit["active_monitors"] == 0

        normal = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        lease2 = (await normal.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor(), "activate"), 0)).unwrap()
        unhealthy = MonitorRegistration("monitor_b", "completion_policy", "pending", False, 0, 5, 0.05)
        denied = await normal.activate(lease2, decision(), unhealthy)
        assert not denied.ok and denied.error.code == "MONITOR_UNHEALTHY"

    asyncio.run(scenario())


def test_stale_active_version_lease_cannot_activate(tmp_path: Path):
    async def scenario():
        admin = governance_admin()
        identity = StaticIdentityProvider((_actor(), _actor("other"), admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        first = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor(), "activate"), 0)).unwrap()
        stale = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor("other"), "activate"), 0)).unwrap()
        await store.activate(first, decision(), MonitorRegistration("m1", "completion_policy", "pending", True, 0, 5, 0.05))

        result = await store.activate(stale, decision("cand_b", active_char="d"), MonitorRegistration("m2", "completion_policy", "pending", True, 0, 5, 0.05))

        assert not result.ok
        assert result.error.code == "ACTIVE_VERSION_CONFLICT"

    asyncio.run(scenario())


def test_activation_rejects_monitor_binding_and_digest_stale_approval(tmp_path: Path):
    async def scenario():
        admin = governance_admin()
        identity = StaticIdentityProvider((_actor(), admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor(), "activate"), 0)).unwrap()
        wrong_monitor = MonitorRegistration("m1", "other_surface", "pending", True, 99, 5, 0.05)

        monitor_denied = await store.activate(lease, decision(), wrong_monitor)

        assert not monitor_denied.ok and monitor_denied.error.code == "MONITOR_BINDING_INVALID"
        stale = replace(decision(approved=True), human_approval=approval(candidate_digest="d" * 64, gate_digest="e" * 64))
        healthy = MonitorRegistration("m2", "completion_policy", "pending", True, 0, 5, 0.05)
        approval_denied = await store.activate(lease, stale, healthy)
        assert not approval_denied.ok and approval_denied.error.code == "APPROVAL_STALE"
        assert (await store.active("completion_policy")).unwrap().version == 0

    asyncio.run(scenario())


def test_activation_accepts_only_a_digest_matching_recorded_independent_review(tmp_path: Path):
    async def scenario():
        operator = _actor()
        approver = _actor("approver", ("governance_approver",))
        admin = governance_admin()
        identity = StaticIdentityProvider((operator, approver, admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        approved = decision(approved=True)
        gate_digest = canonical_digest(approved.gates)
        review = GovernanceReview(new_prefixed_id("review_"), approved.candidate_id, approver, approved.human_approval)
        assert (await store.record_review(review, gate_digest)).ok
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", operator, "activate"), 0)).unwrap()

        result = await store.activate(
            lease,
            approved,
            MonitorRegistration("m1", "completion_policy", "pending", True, 0, 5, 0.05),
        )

        assert result.ok and result.value.version == 1

        await store.initialize_surface("other_surface", artifact("baseline", "b"), admin)
        second = decision("cand_a", approved=True, active_char="d")
        reused = replace(
            second,
            human_approval=approval(
                candidate_digest="d" * 64,
                gate_digest=canonical_digest(second.gates),
            ),
        )
        second_lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "other_surface", operator, "activate"), 0)).unwrap()
        denied = await store.activate(
            second_lease,
            reused,
            MonitorRegistration("m2", "other_surface", "pending", True, 0, 5, 0.05),
        )
        assert not denied.ok and denied.error.code == "APPROVAL_STALE"

    asyncio.run(scenario())


def test_activation_rejects_fabricated_unrecorded_approval(tmp_path: Path):
    async def scenario():
        admin = governance_admin()
        identity = StaticIdentityProvider((_actor(), admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", _actor(), "activate"), 0)).unwrap()

        result = await store.activate(
            lease,
            decision(approved=True),
            MonitorRegistration("m1", "completion_policy", "pending", True, 0, 5, 0.05),
        )

        assert not result.ok and result.error.code == "APPROVAL_STALE"
        assert (await store.active("completion_policy")).unwrap().version == 0

    asyncio.run(scenario())


def test_activation_rejects_lease_actor_substitution_and_unapproved_medium_decision(tmp_path: Path):
    async def scenario():
        operator = _actor()
        substitute = _actor("substitute")
        admin = governance_admin()
        identity = StaticIdentityProvider((operator, substitute, admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", operator, "activate"), 0)).unwrap()

        substituted = await store.activate(
            replace(lease, actor=substitute),
            decision(),
            MonitorRegistration("substitute", "completion_policy", "pending", True, 0, 5, 0.05),
        )
        assert not substituted.ok and substituted.error.code == "GOVERNANCE_LEASE_INVALID"

        medium = replace(decision(), computed_risk=RiskLevel.MEDIUM, matched_risk_rules=("instruction_or_policy:system_prompt",))
        denied = await store.activate(
            lease,
            medium,
            MonitorRegistration("medium", "completion_policy", "pending", True, 0, 5, 0.05),
        )
        assert not denied.ok and denied.error.code in {"AUTHORIZATION_FAILED", "GOVERNANCE_FAILED"}

    asyncio.run(scenario())


def test_authenticated_review_can_activate_high_risk_executable_but_not_high_declarative(tmp_path: Path):
    async def scenario():
        operator = _actor()
        approver = _actor("approver", ("governance_approver",))
        admin = governance_admin()
        identity = StaticIdentityProvider((operator, approver, admin))
        store = SQLiteGovernanceStore(tmp_path / "governance", identity, fixture_artifacts())
        await store.initialize_surface("component", artifact("baseline", "b"), admin)
        executable = replace(
            decision(approved=True),
            computed_risk=RiskLevel.HIGH,
            matched_risk_rules=("executable_component",),
        )
        gate_digest = canonical_digest(executable.gates)
        review = GovernanceReview(new_prefixed_id("review_"), executable.candidate_id, approver, executable.human_approval)
        assert (await store.record_review(review, gate_digest)).ok
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "component", operator, "activate"), 0)).unwrap()

        activated = await store.activate(lease, executable, MonitorRegistration("exec", "component", "pending", True, 0, 5, 0.05))

        assert activated.ok

        await store.initialize_surface("declarative", artifact("baseline", "b"), admin)
        declarative = replace(executable, decision_id=new_prefixed_id("decision_"), matched_risk_rules=("unknown_surface:x",))
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "declarative", operator, "activate"), 0)).unwrap()
        denied = await store.activate(lease, declarative, MonitorRegistration("decl", "declarative", "pending", True, 0, 5, 0.05))
        assert not denied.ok and denied.error.code == "GOVERNANCE_FAILED"

    asyncio.run(scenario())
