from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from loom.campaigns.contracts import ArtifactRef, ExperimentPhase
from loom.campaigns.controller import CampaignController, publish_phase_results
from loom.campaigns.experiments import load_experiment_bundle, publish_experiment_bundle
from loom.campaigns.operations import BudgetReservation, CampaignOperation
from loom.campaigns.proposer import ProposalBatch, ProposalUsage
from loom.campaigns.serialization import canonical_digest, new_prefixed_id
from loom.core import ok
from loom.evaluation.experiments import TrialEntry, TrialPlan

from .conftest import campaign_actor, make_campaign_spec, make_campaign_store, make_candidate_evidence


async def _record(controller, store, spec, candidate_id, *, valid=True, primary=0.8):
    candidate_ref, validation_ref, experiment_ref = make_candidate_evidence(
        store,
        spec,
        candidate_id,
        valid=valid,
        primary=primary,
    )
    return await controller.record_candidate(
        candidate_id,
        candidate_ref=candidate_ref,
        validation_ref=validation_ref,
        experiment_ref=experiment_ref,
    )


def test_controller_seals_search_once_and_selects_entrants_without_operator_substitution(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store, discovery_experiments=3, discovery_runs=30)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        controller = CampaignController(
            store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )

        await _record(controller, store, spec, "cand_b", primary=0.7)
        await _record(controller, store, spec, "cand_a", primary=0.8)
        await _record(controller, store, spec, "cand_failed_gate", primary=0.4)
        await _record(controller, store, spec, "cand_invalid", valid=False)
        seal = await controller.seal_search(operation_id=new_prefixed_id("op_"), expect_frontier_digest=controller.frontier_digest())

        assert seal.ok
        assert seal.value.validation_entrants == ("cand_a",)
        assert (await store.load(spec.campaign_id)).unwrap().latest_frontier_ref is not None
        assert not (await _record(controller, store, spec, "cand_late", primary=1.0)).ok
        assert not (await controller.seal_search(operation_id=new_prefixed_id("op_"), expect_frontier_digest=controller.frontier_digest())).ok

    asyncio.run(scenario())


def test_controller_finalization_is_one_time_and_report_does_not_activate_registry(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        controller = CampaignController(
            store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )
        await _record(controller, store, spec, "cand_a", primary=0.8)
        sealed = await controller.seal_search(new_prefixed_id("op_"), controller.frontier_digest())
        validation = publish_phase_results(
            store,
            spec,
            "validation",
            sealed.value.entrant_digest,
            (make_candidate_evidence(store, spec, "cand_a", primary=0.75, phase=ExperimentPhase.VALIDATION)[2],),
        ).unwrap()
        selected = await controller.select_finalists(validation)
        holdout = publish_phase_results(
            store,
            spec,
            "holdout",
            selected.value.finalist_digest,
            (make_candidate_evidence(store, spec, "cand_a", primary=0.75, phase=ExperimentPhase.HOLDOUT)[2],),
        ).unwrap()
        report = await controller.finalize(
            selected.value.finalist_digest,
            holdout,
            operation_id=new_prefixed_id("op_"),
        )

        assert report.ok
        report_payload = json.loads(store.artifacts.read_bytes(report.value.report_ref).unwrap())
        assert report_payload["attestation"]["subject"] == "campaign_finalizer"
        assert store.artifacts.read_bytes(ArtifactRef(**report_payload["markdown_ref"])).unwrap().startswith(b"# Loom Promotion Recommendation")
        assert report.value.disposition == "recommend"
        assert report.value.active_artifact is None
        assert not (
            await controller.finalize(
                selected.value.finalist_digest,
                holdout,
                operation_id=new_prefixed_id("op_"),
            )
        ).ok

    asyncio.run(scenario())


def test_finalists_are_restricted_to_the_frozen_validation_entrants(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        controller = CampaignController(
            store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )
        await _record(controller, store, spec, "entrant", primary=0.8)
        sealed = await controller.seal_search(new_prefixed_id("op_"), controller.frontier_digest())

        validation = publish_phase_results(
            store,
            spec,
            "validation",
            sealed.value.entrant_digest,
            (make_candidate_evidence(store, spec, "not_an_entrant", primary=1.0, phase=ExperimentPhase.VALIDATION)[2],),
        ).unwrap()
        selected = await controller.select_finalists(validation)

        assert selected.ok and selected.value.candidate_ids == ()

    asyncio.run(scenario())


def test_record_candidate_consumes_discovery_experiment_budget(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        controller = CampaignController(
            store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )

        result = await _record(controller, store, spec, "candidate", primary=0.8)

        assert result.ok
        usage = (await store.budget_usage(spec.campaign_id)).unwrap()
        assert usage.consumed_candidate_experiments["discovery"] == 1
        assert usage.consumed_task_side_runs["discovery"] == 12

    asyncio.run(scenario())


def test_controller_validates_an_already_admitted_manual_candidate_without_duplicate_creation(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("start"),
                "campaign.started",
                campaign_actor("campaign_operator"),
            ),
            1,
        )
        candidate_ref, validation_ref, experiment_ref = make_candidate_evidence(store, spec, "manual")
        admitted = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest(candidate_ref),
                "candidate.created",
                actor=campaign_actor("campaign_operator"),
                payload={"candidate_id": "manual", "candidate_ref": candidate_ref},
                output_refs=(candidate_ref,),
                reservation=BudgetReservation(ExperimentPhase.DISCOVERY, candidates=1),
            ),
            2,
        )
        controller = CampaignController(
            store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )

        recorded = await controller.record_candidate(
            "manual",
            candidate_ref=candidate_ref,
            validation_ref=validation_ref,
            experiment_ref=experiment_ref,
        )
        usage = (await store.budget_usage(spec.campaign_id)).unwrap()

        assert admitted.ok and recorded.ok
        assert usage.candidates == 1 and usage.consumed_candidate_experiments["discovery"] == 1

    asyncio.run(scenario())


def test_controller_rejects_a_candidate_selected_trial_plan(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("start"),
                "campaign.started",
                campaign_actor("campaign_operator"),
            ),
            1,
        )
        candidate_ref, validation_ref, experiment_ref = make_candidate_evidence(store, spec, "candidate")
        bundle = load_experiment_bundle(store, experiment_ref).unwrap()
        entries = list(bundle.trial_plan.entries)
        entries[0] = TrialEntry("f" * 64, entries[0].seed, entries[0].repetition)
        changed = replace(bundle, trial_plan=TrialPlan(tuple(entries)))
        changed_ref = publish_experiment_bundle(store, changed, actor=campaign_actor("campaign_controller")).unwrap()
        controller = CampaignController(
            store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )

        result = await controller.record_candidate(
            "candidate",
            candidate_ref=candidate_ref,
            validation_ref=validation_ref,
            experiment_ref=changed_ref,
        )

        assert not result.ok and result.error.code == "CAMPAIGN_OPERATION_INVALID"

    asyncio.run(scenario())


def test_controller_restores_frontier_seal_and_finalists_after_restart(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        actors = {
            "controller_actor": campaign_actor("campaign_controller"),
            "finalizer_actor": campaign_actor("campaign_finalizer"),
        }
        first = CampaignController(store, spec, **actors)
        await _record(first, store, spec, "cand_a", primary=0.8)
        expected_frontier = first.frontier_digest()

        after_discovery_restart = CampaignController(store, spec, **actors)
        restored = await after_discovery_restart.restore()
        sealed = await after_discovery_restart.seal_search(new_prefixed_id("op_"), expected_frontier)
        assert restored.ok and after_discovery_restart.frontier_digest() == expected_frontier
        assert sealed.ok and sealed.value.validation_entrants == ("cand_a",)

        after_seal_restart = CampaignController(store, spec, **actors)
        validation = publish_phase_results(
            store,
            spec,
            "validation",
            sealed.value.entrant_digest,
            (make_candidate_evidence(store, spec, "cand_a", primary=0.75, phase=ExperimentPhase.VALIDATION)[2],),
        ).unwrap()
        selected = await after_seal_restart.select_finalists(validation)
        assert selected.ok

        after_selection_restart = CampaignController(store, spec, **actors)
        holdout = publish_phase_results(
            store,
            spec,
            "holdout",
            selected.value.finalist_digest,
            (make_candidate_evidence(store, spec, "cand_a", primary=0.75, phase=ExperimentPhase.HOLDOUT)[2],),
        ).unwrap()
        report = await after_selection_restart.finalize(
            selected.value.finalist_digest,
            holdout,
            operation_id=new_prefixed_id("op_"),
        )
        assert report.ok and report.value.candidate_id == "cand_a"

    asyncio.run(scenario())


def test_fixed_proposer_iteration_is_budgeted_and_restart_deterministic(tmp_path: Path):
    class FakeProposer:
        def __init__(self):
            self.calls = 0

        async def propose(self, request, history, workspace):
            del request, history, workspace
            self.calls += 1
            return ok(ProposalBatch(({"draft": "stable"},), ProposalUsage(20, "0.5", 2)))

    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)
        await store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        await store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        actors = {
            "controller_actor": campaign_actor("campaign_controller"),
            "finalizer_actor": campaign_actor("campaign_finalizer"),
        }
        first_proposer = FakeProposer()
        first = CampaignController(store, spec, **actors)

        async def evaluate(draft, candidate_id):
            del draft
            return ok(make_candidate_evidence(store, spec, candidate_id, primary=0.8))

        candidate_ids = await first.run_iteration(
            1,
            first_proposer,
            history=object(),
            workspace=object(),
            evaluate_draft=evaluate,
        )
        second_proposer = FakeProposer()
        replayed = await CampaignController(store, spec, **actors).run_iteration(
            1,
            second_proposer,
            history=object(),
            workspace=object(),
            evaluate_draft=evaluate,
        )

        assert candidate_ids.ok and replayed.unwrap() == candidate_ids.unwrap()
        assert first_proposer.calls == 1 and second_proposer.calls == 0
        usage = (await store.budget_usage(spec.campaign_id)).unwrap()
        assert usage.iterations == 1 and usage.candidates == 1
        assert usage.proposer_tokens == 20 and usage.solver_tokens == 100
        assert usage.cost == "0.6" and usage.wall_time_seconds == 12

    asyncio.run(scenario())


def test_paused_campaign_rejects_new_iterations_and_candidate_admission(tmp_path: Path):
    class NeverProposer:
        async def propose(self, request, history, workspace):  # pragma: no cover - pause must fence this call
            raise AssertionError("paused campaign must not call proposer")

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
                campaign_actor("campaign_operator"),
            ),
            1,
        )
        await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("pause"),
                "campaign.paused",
                campaign_actor("campaign_operator"),
            ),
            started.unwrap().aggregate_version,
        )
        controller = CampaignController(
            store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )

        candidate = await _record(controller, store, spec, "paused_candidate")
        iteration = await controller.run_iteration(
            1,
            NeverProposer(),
            object(),
            object(),
            lambda *_: ok(None),
        )

        assert not candidate.ok and candidate.error.code == "CAMPAIGN_OPERATION_INVALID"
        assert not iteration.ok and iteration.error.code == "CAMPAIGN_OPERATION_INVALID"

    asyncio.run(scenario())
