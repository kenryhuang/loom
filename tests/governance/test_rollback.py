from __future__ import annotations

import asyncio
from pathlib import Path

from loom.campaigns.serialization import new_prefixed_id
from loom.governance.policy import ActorAssertion, GovernanceOperation, StaticIdentityProvider
from loom.governance.registry import MonitorRegistration, SQLiteGovernanceStore
from loom.governance.rollback import RollbackController

from .conftest import artifact, decision, fixture_artifacts, governance_admin


def _actor():
    return ActorAssertion(
        "operator",
        ("registry_operator",),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        "signed",
    )


def test_manual_rollback_compare_and_swaps_exact_version_to_recorded_prior_artifact(tmp_path: Path):
    async def scenario():
        actor = _actor()
        admin = governance_admin()
        store = SQLiteGovernanceStore(tmp_path / "governance", StaticIdentityProvider((actor, admin)), fixture_artifacts())
        baseline = artifact("baseline", "b")
        await store.initialize_surface("completion_policy", baseline, admin)
        lease = (await store.acquire_lease(GovernanceOperation(new_prefixed_id("op_"), "completion_policy", actor, "activate"), 0)).unwrap()
        await store.activate(lease, decision(), MonitorRegistration("monitor", "completion_policy", "pending", True, 0, 3, 0.05))
        controller = RollbackController(store)

        rolled_back = await controller.rollback(
            "completion_policy",
            expected_active_version=1,
            actor=actor,
            reason="operator regression review",
            operation_id=new_prefixed_id("op_"),
        )

        assert rolled_back.ok
        assert rolled_back.value.version == 2
        assert rolled_back.value.artifact == baseline
        stale = await controller.rollback(
            "completion_policy",
            expected_active_version=1,
            actor=actor,
            reason="stale request",
            operation_id=new_prefixed_id("op_"),
        )
        assert not stale.ok and stale.error.code == "ACTIVE_VERSION_CONFLICT"

    asyncio.run(scenario())
