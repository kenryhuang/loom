from __future__ import annotations

import asyncio
import json
from pathlib import Path

import loom.campaigns.store as store_module
from loom.campaigns.contracts import ExperimentPhase
from loom.campaigns.operations import BudgetReservation, CampaignOperation
from loom.campaigns.serialization import canonical_digest, new_prefixed_id

from .conftest import campaign_actor, make_campaign_spec, make_campaign_store


def test_store_create_load_replay_and_expected_version_conflict(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        created = await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        assert created.ok

        operation_id = new_prefixed_id("op_")
        operation = CampaignOperation(
            operation_id=operation_id,
            campaign_id=spec.campaign_id,
            input_digest=canonical_digest({"action": "start"}),
            event_type="campaign.started",
            actor=campaign_actor("campaign_operator"),
        )
        first = await store.transact(operation, expected_version=1)
        replay = await store.transact(operation, expected_version=1)
        stale = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest({"action": "pause"}),
                "campaign.paused",
                campaign_actor("campaign_operator"),
            ),
            expected_version=1,
        )

        assert first.ok and replay.ok
        assert replay.value == first.value
        assert not stale.ok and stale.error.code == "CONCURRENCY_CONFLICT"
        loaded = (await store.load(spec.campaign_id)).unwrap()
        assert loaded.aggregate_version == 2
        assert loaded.event_count == 2

    asyncio.run(scenario())


def test_store_rejects_reused_operation_id_with_different_input(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        operation_id = new_prefixed_id("op_")
        first = CampaignOperation(operation_id, spec.campaign_id, "a" * 64, "campaign.started", campaign_actor("campaign_operator"))
        different = CampaignOperation(operation_id, spec.campaign_id, "b" * 64, "campaign.started", campaign_actor("campaign_operator"))

        assert (await store.transact(first, 1)).ok
        result = await store.transact(different, 2)

        assert not result.ok
        assert result.error.code == "OPERATION_CONFLICT"

    asyncio.run(scenario())


def test_two_store_instances_cannot_overspend_phase_budget(tmp_path: Path):
    async def setup():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store, discovery_experiments=1, discovery_runs=2)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        return spec

    spec = asyncio.run(setup())

    async def reserve(operation_id: str):
        store = make_campaign_store(tmp_path / "campaign")
        projection = (await store.load(spec.campaign_id)).unwrap()
        return await store.transact(
            CampaignOperation(
                operation_id,
                spec.campaign_id,
                canonical_digest(operation_id),
                "experiment.started",
                actor=campaign_actor("campaign_controller"),
                reservation=BudgetReservation(ExperimentPhase.DISCOVERY, candidate_experiments=1, task_side_runs=2),
            ),
            projection.aggregate_version,
        )

    async def race():
        return await asyncio.gather(
            asyncio.to_thread(asyncio.run, reserve(new_prefixed_id("op_"))),
            asyncio.to_thread(asyncio.run, reserve(new_prefixed_id("op_"))),
        )

    results = asyncio.run(race())

    assert sum(result.ok for result in results) == 1
    failure = next(result for result in results if not result.ok)
    assert failure.error.code in {"BUDGET_EXCEEDED", "CONCURRENCY_CONFLICT"}


def test_expired_lease_reconciliation_releases_reservation_once(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store, discovery_experiments=1, discovery_runs=2)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        started = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("lease"),
                "experiment.started",
                actor=campaign_actor("campaign_controller"),
                reservation=BudgetReservation(ExperimentPhase.DISCOVERY, candidate_experiments=1, task_side_runs=2),
                complete=False,
                lease_seconds=0,
            ),
            1,
        )
        assert started.ok

        first = await store.reconcile(started.value.lease_id, campaign_actor("campaign_controller"))
        second = await store.reconcile(started.value.lease_id, campaign_actor("campaign_controller"))
        fenced_worker = await store.complete_lease(started.value.lease_id, campaign_actor("campaign_controller"))

        assert first.ok and first.value.action == "released"
        assert second.ok and second.value.action == "already_reconciled"
        assert not fenced_worker.ok and fenced_worker.error.code == "OPERATION_CONFLICT"
        usage = await store.budget_usage(spec.campaign_id)
        assert usage.unwrap().reserved_candidate_experiments[ExperimentPhase.DISCOVERY.value] == 0

    asyncio.run(scenario())


def test_cancelled_incomplete_operation_can_reacquire_lease_without_duplicate_event(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store, discovery_experiments=1, discovery_runs=2)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        actor = campaign_actor("campaign_controller")
        operation = CampaignOperation(
            new_prefixed_id("op_"),
            spec.campaign_id,
            canonical_digest("retryable-lease"),
            "experiment.started",
            actor=actor,
            reservation=BudgetReservation(ExperimentPhase.DISCOVERY, candidate_experiments=1, task_side_runs=2),
            complete=False,
        )
        first = await store.transact(operation, 1)
        cancelled = await store.cancel_lease(first.unwrap().lease_id, actor)

        retried = await store.transact(operation, 2)
        completed = await store.complete_lease(retried.unwrap().lease_id, actor)
        events = (await store.events(spec.campaign_id)).unwrap()

        assert cancelled.unwrap().action == "cancelled"
        assert retried.ok and retried.value.lease_id != first.value.lease_id
        assert completed.unwrap().action == "completed"
        assert [event.event_type for event in events].count("experiment.started") == 1

    asyncio.run(scenario())


def test_manifest_write_failure_rolls_back_create_and_retry_repairs_manifest(tmp_path: Path, monkeypatch):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        original = store_module._atomic_write

        def fail_once(path, content):
            monkeypatch.setattr(store_module, "_atomic_write", original)
            raise OSError("injected manifest failure")

        monkeypatch.setattr(store_module, "_atomic_write", fail_once)
        failed = await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        retried = await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))

        assert not failed.ok and failed.error.code == "CAMPAIGN_STORE_FAILED"
        assert retried.ok
        assert (tmp_path / "campaign" / "campaign.json").read_bytes() == store_module.canonical_json_bytes(spec)

    asyncio.run(scenario())


def test_active_lease_completion_moves_all_reservations_to_consumed_once(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store, discovery_experiments=1, discovery_runs=2)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        started = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("lease-complete"),
                "experiment.started",
                actor=campaign_actor("campaign_controller"),
                reservation=BudgetReservation(
                    ExperimentPhase.DISCOVERY,
                    iterations=1,
                    candidates=1,
                    candidate_experiments=1,
                    task_side_runs=2,
                    proposer_tokens=10,
                    solver_tokens=20,
                    cost="1.25",
                    wall_time_seconds=3,
                ),
                complete=False,
            ),
            1,
        )

        completed = await store.complete_lease(started.unwrap().lease_id, campaign_actor("campaign_controller"))
        replay = await store.complete_lease(started.unwrap().lease_id, campaign_actor("campaign_controller"))
        usage = (await store.budget_usage(spec.campaign_id)).unwrap()

        assert completed.unwrap().action == "completed"
        assert replay.unwrap().action == "already_reconciled"
        assert usage.consumed_candidate_experiments["discovery"] == 1
        assert usage.reserved_candidate_experiments["discovery"] == 0
        assert (usage.iterations, usage.candidates, usage.proposer_tokens, usage.solver_tokens) == (1, 1, 10, 20)
        assert usage.cost == "1.25" and usage.wall_time_seconds == 3

    asyncio.run(scenario())


def test_lease_completion_consumes_measured_usage_and_releases_unused_reservation(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        actor = campaign_actor("campaign_controller")
        started = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("measured-lease"),
                "experiment.started",
                actor=actor,
                reservation=BudgetReservation(
                    ExperimentPhase.DISCOVERY,
                    iterations=1,
                    proposer_tokens=100,
                    cost="1.00",
                    wall_time_seconds=30,
                ),
                complete=False,
            ),
            1,
        )
        actual = BudgetReservation(
            ExperimentPhase.DISCOVERY,
            iterations=1,
            proposer_tokens=17,
            cost="0.12",
            wall_time_seconds=4,
        )

        completed = await store.complete_lease(started.unwrap().lease_id, actor, actual)
        usage = (await store.budget_usage(spec.campaign_id)).unwrap()

        assert completed.ok
        assert usage.proposer_tokens == 17 and usage.reserved_proposer_tokens == 0
        assert usage.cost == "0.12" and usage.reserved_cost == "0"
        assert usage.wall_time_seconds == 4 and usage.reserved_wall_time_seconds == 0

    asyncio.run(scenario())


def test_export_rebuilds_projection_and_corrupt_artifact_freezes_load(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        ref = store.artifacts.publish_bytes(b"evidence", kind="evidence", schema_version="evidence.v1").unwrap()
        await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest({"ref": ref}),
                "experience.imported",
                actor=campaign_actor("campaign_creator"),
                payload={"artifact_ref": ref},
            ),
            1,
        )

        exported = (await store.export(spec.campaign_id)).unwrap()
        export_payload = json.loads(store.artifacts.read_bytes(exported).unwrap())
        rebuilt = (await store.rebuild(spec.campaign_id)).unwrap()
        assert export_payload["projection"] == rebuilt.to_dict()

        store.artifacts.resolve(ref).unwrap().write_bytes(b"corrupt")
        failed = await store.load(spec.campaign_id)
        assert not failed.ok
        assert failed.error.code == "ARTIFACT_INTEGRITY_FAILED"

    asyncio.run(scenario())


def test_store_detects_truncated_event_tail_against_committed_version(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        connection = store._connect()
        try:
            connection.execute("DELETE FROM events WHERE sequence = 2")
        finally:
            connection.close()

        result = await store.load(spec.campaign_id)

        assert not result.ok and result.error.code == "CAMPAIGN_EVENT_INVALID"

    asyncio.run(scenario())


def test_store_enforces_global_token_retry_and_cost_budgets(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        oversized = BudgetReservation(
            ExperimentPhase.DISCOVERY,
            infrastructure_retry_task_side_runs=1,
            proposer_tokens=1001,
            solver_tokens=2001,
            cost="10.01",
        )

        result = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("oversized"),
                "experiment.started",
                actor=campaign_actor("campaign_controller"),
                reservation=oversized,
            ),
            1,
        )

        assert not result.ok and result.error.code == "BUDGET_EXCEEDED"
        usage = (await store.budget_usage(spec.campaign_id)).unwrap()
        assert usage.proposer_tokens == 0 and usage.solver_tokens == 0 and usage.cost == "0"

    asyncio.run(scenario())


def test_store_rejects_invalid_transition_before_committing_event(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        started = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("start"),
                "campaign.started",
                actor=campaign_actor("campaign_operator"),
            ),
            1,
        )
        created = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("candidate"),
                "candidate.created",
                actor=campaign_actor("campaign_operator"),
                payload={"candidate_id": "candidate"},
            ),
            started.unwrap().aggregate_version,
        )
        promoted = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("promoted"),
                "candidate.promoted",
                actor=campaign_actor("campaign_controller"),
                payload={"candidate_id": "candidate"},
            ),
            created.unwrap().aggregate_version,
        )
        finalized = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("final"),
                "campaign.finalized",
                campaign_actor("campaign_finalizer"),
            ),
            created.unwrap().aggregate_version,
        )

        assert not promoted.ok and not finalized.ok
        projection = (await store.load(spec.campaign_id)).unwrap()
        assert projection.aggregate_version == created.unwrap().aggregate_version
        assert projection.candidates["candidate"].lifecycle.value == "proposed"

    asyncio.run(scenario())


def test_rejected_second_create_does_not_overwrite_campaign_manifest(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        first = make_campaign_spec(store)
        await store.create(first, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        manifest = (tmp_path / "campaign" / "campaign.json").read_bytes()
        second = make_campaign_spec(store)

        rejected = await store.create(second, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))

        assert not rejected.ok and rejected.error.code == "CAMPAIGN_ALREADY_EXISTS"
        assert (tmp_path / "campaign" / "campaign.json").read_bytes() == manifest

    asyncio.run(scenario())
