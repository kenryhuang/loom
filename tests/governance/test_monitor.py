from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from loom.campaigns.serialization import new_prefixed_id
from loom.governance.monitor import MonitoringService
from loom.governance.policy import ActorAssertion, GovernanceOperation, StaticIdentityProvider
from loom.governance.registry import MonitorRegistration, SQLiteGovernanceStore

from .conftest import artifact, decision, fixture_artifacts, governance_admin


def _operator():
    return ActorAssertion(
        "automation",
        ("registry_operator", "governance_automation"),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "signed",
    )


def _monitor():
    return ActorAssertion(
        "monitor",
        ("monitor_service",),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "monitor-signed",
    )


def test_monitoring_regression_rolls_back_exact_observed_version(tmp_path: Path):
    async def scenario():
        actor, monitor_actor = _operator(), _monitor()
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((actor, monitor_actor, admin)), fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", actor, "activate"), 0)).unwrap()
        active = (
            await store.activate(
                lease,
                decision(approved=False),
                MonitorRegistration("monitor_a", "completion_policy", "pending", True, 0, 3, 0.05),
            )
        ).unwrap()
        monitor = MonitoringService(store, monitor_actor)

        assert (await monitor.record("monitor_a", active.version, quality_delta=-0.10)).ok
        assert (await monitor.record("monitor_a", active.version, quality_delta=-0.08)).ok
        requested = await monitor.record("monitor_a", active.version, quality_delta=-0.09)

        assert requested.ok and requested.value.action == "rollback_requested"
        rolled_back = await store.execute_monitor_rollback("monitor_a", active.version, actor)
        assert rolled_back.ok and rolled_back.value.action == "rolled_back"
        restored = (await store.active("completion_policy")).unwrap()
        assert restored.artifact.sha256 == "b" * 64
        assert restored.version == 2

    asyncio.run(scenario())


def test_stale_monitor_closes_without_rolling_back_newer_version(tmp_path: Path):
    async def scenario():
        actor, monitor_actor = _operator(), _monitor()
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((actor, monitor_actor, admin)), fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease1 = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", actor, "activate"), 0)).unwrap()
        first = (await store.activate(lease1, decision(approved=False), MonitorRegistration("old", "completion_policy", "pending", True, 0, 1, 0.01))).unwrap()
        lease2 = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", actor, "activate"), 1)).unwrap()
        second = (
            await store.activate(
                lease2,
                decision("cand_b", approved=False, active_char="d", rollback_char="c"),
                MonitorRegistration("new", "completion_policy", "pending", True, 1, 2, 0.05),
            )
        ).unwrap()
        monitor = MonitoringService(store, monitor_actor)

        result = await monitor.record("old", first.version, quality_delta=-1.0)

        assert result.ok and result.value.action == "stale_closed"
        assert (await store.active("completion_policy")).unwrap() == second

    asyncio.run(scenario())


def test_monitor_service_can_request_but_cannot_mutate_registry_pointer(tmp_path: Path):
    async def scenario():
        operator = _operator()
        monitor_actor = ActorAssertion(
            "monitor-only",
            ("monitor_service",),
            "2026-07-18T00:00:00.000000Z",
            "2999-01-01T00:00:00.000000Z",
            "monitor-signed",
        )
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((operator, monitor_actor, admin)), fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", operator, "activate"), 0)).unwrap()
        active = (
            await store.activate(
                lease,
                decision(approved=False),
                MonitorRegistration("monitor-only", "completion_policy", "pending", True, 0, 1, 0.01),
            )
        ).unwrap()

        requested = await MonitoringService(store, monitor_actor).record("monitor-only", active.version, quality_delta=-1.0)

        assert requested.ok and requested.value.action == "rollback_requested"
        assert (await store.active("completion_policy")).unwrap().version == active.version
        executed = await store.execute_monitor_rollback("monitor-only", active.version, operator)
        assert executed.ok and executed.value.action == "rolled_back"
        assert (await store.active("completion_policy")).unwrap().artifact.sha256 == "b" * 64

    asyncio.run(scenario())


def test_wrong_sample_version_is_rejected_without_closing_current_monitor(tmp_path: Path):
    async def scenario():
        actor, monitor_actor = _operator(), _monitor()
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((actor, monitor_actor, admin)), fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", actor, "activate"), 0)).unwrap()
        await store.activate(
            lease,
            decision(approved=False),
            MonitorRegistration("monitor", "completion_policy", "pending", True, 0, 3, 0.05),
        )

        wrong = await MonitoringService(store, monitor_actor).record("monitor", 999, quality_delta=-1.0)

        assert not wrong.ok and wrong.error.code == "MONITOR_BINDING_INVALID"
        assert (await store.audit("completion_policy")).unwrap()["active_monitors"] == 1

    asyncio.run(scenario())


def test_monitor_ttl_fails_closed_without_enough_evidence_and_heartbeat_is_version_bound(tmp_path: Path):
    async def scenario():
        operator = _operator()
        monitor_actor = ActorAssertion(
            "ttl-monitor",
            ("monitor_service",),
            "2026-07-18T00:00:00.000000Z",
            "2999-01-01T00:00:00.000000Z",
            "ttl-signed",
        )
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((operator, monitor_actor, admin)), fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", operator, "activate"), 0)).unwrap()
        active = (
            await store.activate(
                lease,
                decision(approved=False),
                MonitorRegistration("ttl", "completion_policy", "pending", True, 0, 3, 0.05, ttl_runs=2),
            )
        ).unwrap()

        assert (await store.heartbeat_monitor("ttl", active.version, monitor_actor)).ok
        wrong_heartbeat = await store.heartbeat_monitor("ttl", 999, monitor_actor)
        assert not wrong_heartbeat.ok and wrong_heartbeat.error.code == "MONITOR_BINDING_INVALID"
        assert (await MonitoringService(store, monitor_actor).record("ttl", active.version, quality_delta=0.0)).ok
        expired = await MonitoringService(store, monitor_actor).record("ttl", active.version, quality_delta=0.0)

        assert expired.ok and expired.value.action == "rollback_requested"
        assert (await store.active("completion_policy")).unwrap().version == active.version

    asyncio.run(scenario())


def test_heartbeat_timeout_automatically_rolls_back_through_governance_automation(tmp_path: Path):
    async def scenario():
        actor = _operator()
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((actor, admin)), fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", actor, "activate"), 0)).unwrap()
        active = (
            await store.activate(
                lease,
                decision(approved=False),
                MonitorRegistration("heartbeat", "completion_policy", "pending", True, 0, 3, 0.05, heartbeat_grace_seconds=1),
            )
        ).unwrap()

        actions = await store.sweep_stale_heartbeats("2099-01-01T00:00:00.000000Z", actor)

        assert actions.ok and actions.value[0].action == "rolled_back"
        restored = (await store.active("completion_policy")).unwrap()
        assert restored.version == active.version + 1 and restored.artifact.sha256 == "b" * 64

    asyncio.run(scenario())


def test_monitor_ttl_requests_expiry_even_after_sufficient_healthy_samples(tmp_path: Path):
    async def scenario():
        operator, monitor_actor = _operator(), _monitor()
        store = SQLiteGovernanceStore(
            tmp_path / "governance",
            StaticIdentityProvider((operator, monitor_actor, governance_admin())),
            fixture_artifacts(),
        )
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), governance_admin())
        lease = (
            await store.acquire_lease(
                GovernanceOperation(new_prefixed_id("op_"), "completion_policy", operator, "activate"),
                0,
            )
        ).unwrap()
        active = (
            await store.activate(
                lease,
                decision(approved=False),
                MonitorRegistration("ttl-healthy", "completion_policy", "pending", True, 0, 1, 0.05, ttl_runs=2),
            )
        ).unwrap()

        first = await MonitoringService(store, monitor_actor).record("ttl-healthy", active.version, quality_delta=0.1)
        expired = await MonitoringService(store, monitor_actor).record("ttl-healthy", active.version, quality_delta=0.1)

        assert first.unwrap().action == "observed"
        assert expired.unwrap().action == "rollback_requested"

    asyncio.run(scenario())


def test_monitor_rejects_non_finite_evidence_without_poisoning_aggregate(tmp_path: Path):
    async def scenario():
        operator, monitor_actor = _operator(), _monitor()
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((operator, monitor_actor, admin)), fixture_artifacts())
        await store.initialize_surface("completion_policy", artifact("baseline", "b"), admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", operator, "activate"), 0)).unwrap()
        active = (
            await store.activate(
                lease,
                decision(approved=False),
                MonitorRegistration("finite", "completion_policy", "pending", True, 0, 2, 0.05),
            )
        ).unwrap()

        denied = await MonitoringService(store, monitor_actor).record("finite", active.version, quality_delta=float("nan"))
        first = await MonitoringService(store, monitor_actor).record("finite", active.version, quality_delta=-0.1)

        assert not denied.ok and denied.error.code == "GOVERNANCE_FAILED"
        assert first.ok and first.value.action == "observed"

    asyncio.run(scenario())


@pytest.mark.parametrize("threshold", [math.nan, math.inf, -math.inf])
def test_monitor_registration_rejects_non_finite_thresholds(threshold: float):
    with pytest.raises(ValueError, match="finite"):
        MonitorRegistration("finite-policy", "completion_policy", "pending", True, 0, 2, threshold)


@pytest.mark.parametrize("ceiling", [math.nan, math.inf, -1.0])
def test_monitor_registration_rejects_invalid_resource_ceilings(ceiling: float):
    with pytest.raises(ValueError, match="resource ceilings"):
        MonitorRegistration(
            "finite-resources",
            "completion_policy",
            "pending",
            True,
            0,
            2,
            0.05,
            resource_ceilings={"total_tokens": ceiling},
        )
