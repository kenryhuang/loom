from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from loom.core import err, make_loom_error, ok
from loom.optimize.contracts import OptimizationLifecycle, OptimizationSpec, OptimizationStage
from loom.optimize.control import OptimizeRunControl
from loom.optimize.orchestrator import OptimizeCampaignServices, OptimizeOrchestrator
from loom.optimize.store import SQLiteOptimizationStore


class RecordingObserver:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class FakeCampaignServices:
    def __init__(self, *, entrants=("cand-a", "cand-b"), finalists=("cand-a",)):
        self.calls = Counter()
        self.entrants = tuple(entrants)
        self.finalists = tuple(finalists)
        self.failures: Counter[str] = Counter()
        self.holdout_cohorts = []

    def fail_once_at(self, name: str):
        self.failures[name] += 1

    def _start(self, name: str):
        self.calls[name] += 1
        if self.failures[name]:
            self.failures[name] -= 1
            return err(make_loom_error("TEST_FAILURE", f"fail once at {name}", retryable=True))
        return None

    async def preflight(self):
        failed = self._start("preflight")
        return failed or ok({"preflight_digest": "preflight-sha"})

    async def seed_evaluation(self):
        failed = self._start("seed_evaluation")
        return failed or ok({"evaluation_ref": "evaluation-sha"})

    async def seed_evolution(self, evaluation):
        failed = self._start("seed_evolution")
        return failed or ok({"evolution_ref": f"evolution-for-{evaluation['evaluation_ref']}"})

    async def initialize_campaign(self, seed):
        failed = self._start("initialize_campaign")
        return failed or ok({"campaign_id": "cmp_test", "seed": seed})

    async def search_iteration(self, iteration: int):
        failed = self._start("search")
        candidates = ("cand-a",) if iteration == 1 else ("cand-b",)
        return failed or ok({"candidate_ids": candidates})

    async def seal_search(self, candidate_ids):
        failed = self._start("seal_search")
        return failed or ok({"entrant_ids": self.entrants, "candidate_ids": tuple(candidate_ids), "entrant_digest": "entrants-sha"})

    async def validate(self, entrant_ids):
        failed = self._start("validation")
        return failed or ok({"validated_ids": tuple(entrant_ids), "experiment_refs": ("validation-ref",)})

    async def select_finalists(self, validated_ids):
        failed = self._start("select_finalists")
        assert set(self.finalists).issubset(validated_ids)
        return failed or ok({"finalist_ids": self.finalists, "finalist_digest": "finalists-sha"})

    async def holdout(self, finalist_ids):
        failed = self._start("holdout")
        self.holdout_cohorts.append(tuple(finalist_ids))
        return failed or ok({"evaluated_ids": tuple(finalist_ids), "experiment_refs": ("holdout-ref",)})

    async def finalize(self, finalist_ids, holdout):
        failed = self._start("finalize")
        disposition = "recommend" if finalist_ids else "reject"
        return failed or ok(
            {
                "disposition": disposition,
                "candidate_id": finalist_ids[0] if finalist_ids else None,
                "recommendation_ref": "recommendation-sha",
                "holdout": holdout,
            }
        )

    def contract(self) -> OptimizeCampaignServices:
        return OptimizeCampaignServices(
            preflight=self.preflight,
            seed_evaluation=self.seed_evaluation,
            seed_evolution=self.seed_evolution,
            initialize_campaign=self.initialize_campaign,
            search_iteration=self.search_iteration,
            seal_search=self.seal_search,
            validate=self.validate,
            select_finalists=self.select_finalists,
            holdout=self.holdout,
            finalize=self.finalize,
        )


def _spec(tmp_path: Path) -> OptimizationSpec:
    trace = tmp_path / "seed.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    return OptimizationSpec(
        "loom.optimization.spec.v1",
        "opt_test",
        "a" * 64,
        trace,
        "b" * 64,
        {"discovery": "c" * 64, "validation": "d" * 64, "holdout": "e" * 64},
        {"proposer": "f" * 64, "solver": "1" * 64, "judge": "2" * 64},
        "cmp_test",
    )


def _fixture(tmp_path: Path, *, control=None, observer=None, **service_options):
    spec = _spec(tmp_path)
    store = SQLiteOptimizationStore(tmp_path / "optimization")
    services = FakeCampaignServices(**service_options)
    observer = observer or RecordingObserver()
    orchestrator = OptimizeOrchestrator(spec, store, services.contract(), iterations=2, observer=observer, control=control)
    return orchestrator, store, services, observer


@pytest.mark.asyncio
async def test_orchestrator_runs_seed_search_validation_and_holdout(tmp_path: Path):
    orchestrator, store, services, observer = _fixture(tmp_path)

    outcome = (await orchestrator.run_campaign()).unwrap()
    state = (await store.load(outcome.optimization_id)).unwrap()

    assert state.stage is OptimizationStage.HOLDOUT_COMPLETE
    assert outcome.disposition == "recommend"
    assert services.calls["seed_evaluation"] == 1
    assert services.calls["seed_evolution"] == 1
    assert services.calls["holdout"] == 1
    assert services.holdout_cohorts == [("cand-a",)]
    stages = [event["stage"] for event in observer.events if event["status"] == "completed"]
    assert stages == [
        "preflight_complete",
        "seed_analysis_complete",
        "campaign_initialized",
        "search_running",
        "search_running",
        "search_sealed",
        "validation_complete",
        "finalists_selected",
        "holdout_complete",
    ]


@pytest.mark.asyncio
async def test_orchestrator_emits_stage_lifecycle_at_durable_boundaries(tmp_path: Path):
    orchestrator, _store, _services, observer = _fixture(tmp_path)

    await orchestrator.run_campaign()

    assert [(event["type"], event["status"]) for event in observer.events[:2]] == [
        ("optimization.stage.started", "running"),
        ("optimization.stage.completed", "completed"),
    ]
    assert observer.events[0]["payload"]["lease_id"]
    assert observer.events[1]["payload"]["aggregate_version"] >= 1


@pytest.mark.asyncio
async def test_orchestrator_emits_failed_after_stage_lease_is_cancelled(tmp_path: Path):
    orchestrator, _store, services, observer = _fixture(tmp_path)
    services.fail_once_at("preflight")

    result = await orchestrator.run_campaign()

    assert not result.ok
    assert [(event["type"], event["status"]) for event in observer.events] == [
        ("optimization.stage.started", "running"),
        ("optimization.stage.failed", "failed"),
    ]


@pytest.mark.asyncio
async def test_orchestrator_emits_replayed_without_repeating_completed_service(tmp_path: Path):
    orchestrator, _store, services, observer = _fixture(tmp_path)
    services.fail_once_at("search")
    await orchestrator.run_campaign()
    observer.events.clear()

    result = await orchestrator.run_campaign()

    assert result.ok
    assert observer.events[0]["type"] == "optimization.stage.replayed"
    assert services.calls["preflight"] == 1
    assert services.calls["seed_evaluation"] == 1


@pytest.mark.asyncio
async def test_pause_request_stops_before_next_stage_and_persists_paused_lifecycle(tmp_path: Path):
    control = OptimizeRunControl()

    class PauseAfterPreflight(RecordingObserver):
        def emit(self, event):
            super().emit(event)
            if event["stage"] == "preflight_complete" and event["status"] == "completed":
                control.request_pause("tui pause")

    observer = PauseAfterPreflight()
    orchestrator, store, services, _observer = _fixture(tmp_path, control=control, observer=observer)

    result = await orchestrator.run_campaign()

    assert result.error.code == "OPTIMIZATION_PAUSED"
    assert services.calls["seed_evaluation"] == 0
    assert (await store.load("opt_test")).unwrap().lifecycle is OptimizationLifecycle.PAUSED


@pytest.mark.asyncio
async def test_orchestrator_resume_does_not_repeat_completed_seed_calls(tmp_path: Path):
    orchestrator, store, services, _observer = _fixture(tmp_path)
    services.fail_once_at("search")

    first = await orchestrator.run_campaign()
    second = await orchestrator.run_campaign()

    assert not first.ok and second.ok
    assert services.calls["seed_evaluation"] == 1
    assert services.calls["seed_evolution"] == 1
    events = (await store.events("opt_test")).unwrap()
    completed_ids = [event["operation_id"] for event in events if event["event_type"] == "optimization.stage_completed"]
    assert len(completed_ids) == len(set(completed_ids))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrants", "finalists"),
    [
        ((), ()),
        (("cand-a",), ()),
    ],
    ids=("no-entrants", "no-finalists"),
)
async def test_orchestrator_rejects_without_opening_holdout_when_cohort_is_empty(tmp_path: Path, entrants, finalists):
    orchestrator, store, services, _observer = _fixture(tmp_path, entrants=entrants, finalists=finalists)

    outcome = (await orchestrator.run_campaign()).unwrap()

    assert outcome.disposition == "reject"
    assert services.calls["holdout"] == 0
    assert (await store.load("opt_test")).unwrap().stage is OptimizationStage.HOLDOUT_COMPLETE


@pytest.mark.asyncio
async def test_orchestrator_fails_closed_when_holdout_service_changes_frozen_cohort(tmp_path: Path):
    orchestrator, _store, services, _observer = _fixture(tmp_path)

    async def wrong_holdout(finalist_ids):
        services.calls["holdout"] += 1
        return ok({"evaluated_ids": (*finalist_ids, "cand-hidden"), "experiment_refs": ()})

    orchestrator.services = replace(services.contract(), holdout=wrong_holdout)

    result = await orchestrator.run_campaign()

    assert not result.ok
    assert result.error.code == "OPTIMIZATION_HOLDOUT_COHORT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("governance_disposition", "lifecycle"),
    [
        ("promoted", OptimizationLifecycle.PROMOTED),
        ("awaiting_approval", OptimizationLifecycle.AWAITING_APPROVAL),
    ],
)
async def test_public_run_completes_governance_and_writes_stable_outputs(tmp_path: Path, governance_disposition, lifecycle):
    orchestrator, store, _services, _observer = _fixture(tmp_path)
    governance_calls = []

    async def govern(outcome):
        governance_calls.append(outcome.candidate_id)
        return ok(
            {
                "disposition": governance_disposition,
                "candidate_id": outcome.candidate_id,
                "promotion_decision_ref": {"sha256": "decision-sha"},
                "monitor_ref": {"monitor_id": "monitor-1"} if governance_disposition == "promoted" else None,
            }
        )

    orchestrator.governance = govern
    orchestrator.output_dir = tmp_path / "result"

    result = (await orchestrator.run()).unwrap()
    state = (await store.load("opt_test")).unwrap()

    assert state.stage is OptimizationStage.GOVERNANCE_COMPLETE
    assert state.lifecycle is lifecycle
    assert result.disposition == governance_disposition
    assert result.report_path.is_file()
    report = result.report_path.read_text(encoding="utf-8")
    assert '"seed_analysis"' in report
    assert '"holdout"' in report
    assert '"governance"' in report
    assert (tmp_path / "result" / "result.json").is_file()
    assert governance_calls == ["cand-a"]


@pytest.mark.asyncio
async def test_public_run_maps_empty_campaign_to_terminal_rejection_without_governance_call(tmp_path: Path):
    orchestrator, store, _services, _observer = _fixture(tmp_path, entrants=(), finalists=())
    calls = []

    async def govern(outcome):
        calls.append(outcome)
        return ok({})

    orchestrator.governance = govern
    orchestrator.output_dir = tmp_path / "result"

    result = (await orchestrator.run()).unwrap()

    assert result.disposition == "rejected"
    assert (await store.load("opt_test")).unwrap().lifecycle is OptimizationLifecycle.REJECTED
    assert calls == []
