from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from loom.campaigns.contracts import ArtifactRef, ExperimentPhase, ExperimentStatus, ObjectiveDirection, ObjectiveSpec, TaskSetRef
from loom.campaigns.experiments import (
    BaselineRunCache,
    PairedExperimentRunner,
    TrialExecution,
    ValidatedCandidate,
    aggregate_paired_metric,
    objective_gate_result,
)
from loom.evaluation.experiments import TrialEntry, TrialPlan


def _ref(kind: str, digest: str) -> ArtifactRef:
    return ArtifactRef(f"{kind}.v1", kind, f"{kind}-{digest[:4]}.json", digest, 1)


def _candidate() -> ValidatedCandidate:
    return ValidatedCandidate(
        "cmp_test",
        "cand_test",
        _ref("candidate", "a" * 64),
        _ref("baseline", "b" * 64),
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "f" * 64,
        "1" * 64,
    )


def test_paired_metric_bootstrap_is_deterministic_and_keeps_behavior_failures():
    baseline = (0.5, 0.5, 0.5, 0.5, 0.5)
    candidate = (0.7, 0.7, 0.0, 0.7, 0.7)  # zero is a behavioral result, not missing

    first = aggregate_paired_metric("success", baseline, candidate, experiment_id="exp-fixed")
    second = aggregate_paired_metric("success", baseline, candidate, experiment_id="exp-fixed")

    assert first == second
    assert first.valid_pairs == 5
    assert first.candidate_value == 0.56
    assert first.delta_interval is not None


def test_paired_metric_missing_values_fail_closed_for_hard_objective():
    metric = aggregate_paired_metric("success", (0.5, 0.5, None, 0.5), (0.6, None, 0.6, 0.6), experiment_id="exp-small")
    objective = ObjectiveSpec(
        "success",
        ObjectiveDirection.MAXIMIZE,
        hard=True,
        absolute_limit=0.5,
        max_baseline_regression=0,
        min_valid_pairs=5,
        missing_policy="fail_closed",
    )

    assert metric.valid_pairs == 2
    assert metric.delta_interval is None
    assert objective_gate_result(objective, metric).value == "insufficient_evidence"


def test_objective_gate_uses_exact_maximize_and_minimize_interval_boundaries():
    maximize = ObjectiveSpec(
        "success",
        ObjectiveDirection.MAXIMIZE,
        hard=True,
        absolute_limit=0.6,
        max_baseline_regression=0.02,
        min_valid_pairs=5,
        missing_policy="fail_closed",
    )
    minimize = ObjectiveSpec(
        "tokens",
        ObjectiveDirection.MINIMIZE,
        hard=True,
        absolute_limit=100,
        max_baseline_regression=5,
        min_valid_pairs=5,
        missing_policy="fail_closed",
    )
    success_metric = aggregate_paired_metric("success", (0.6,) * 5, (0.6,) * 5, experiment_id="exp-success")
    token_metric = aggregate_paired_metric("tokens", (100.0,) * 5, (100.0,) * 5, experiment_id="exp-tokens")

    assert objective_gate_result(maximize, success_metric).value == "passed"
    assert objective_gate_result(minimize, token_metric).value == "passed"


def test_runner_executes_fixed_pairs_retries_only_infrastructure_failure_and_never_adds_samples(tmp_path: Path):
    calls: list[tuple[str, str]] = []
    attempts: dict[tuple[str, str], int] = {}

    async def execute(side, _candidate, entry, trial_id):
        key = (side, trial_id)
        calls.append(key)
        attempts[key] = attempts.get(key, 0) + 1
        if side == "candidate" and entry.task_fingerprint == "task-b" and attempts[key] == 1:
            return TrialExecution.infrastructure_failure(
                trial_id,
                side,
                "SANDBOX_START_FAILED",
                solver_tokens=3,
                cost="0.03",
                wall_time_seconds=1,
            )
        value = 0.5 if side == "baseline" else 0.7
        return TrialExecution.success(
            trial_id,
            side,
            {"success": value},
            _ref("evaluation", ("2" if side == "baseline" else "3") * 64),
            _ref("trace", ("4" if side == "baseline" else "5") * 64),
            solver_tokens=10,
            cost="0.10",
            wall_time_seconds=2,
        )

    task_set = TaskSetRef("tasks", ExperimentPhase.DISCOVERY, _ref("taskset", "6" * 64), "7" * 64)
    plan = TrialPlan((TrialEntry("task-a", 1, 0), TrialEntry("task-b", 1, 0)))
    runner = PairedExperimentRunner(execute, objectives=(ObjectiveSpec("success", ObjectiveDirection.MAXIMIZE),), infrastructure_retries=1)

    bundle = asyncio.run(runner.evaluate(_candidate(), task_set, plan, experiment_id="exp-fixed")).unwrap()

    assert bundle.status is ExperimentStatus.COMPLETED
    assert bundle.trial_plan == plan
    assert len(bundle.baseline_runs) == 2
    assert len(bundle.candidate_runs) == 2
    assert len(calls) == 5
    assert sum(1 for side, trial_id in calls if side == "candidate" and "task-b" in trial_id) == 2
    assert bundle.metrics[0].valid_pairs == 2
    assert bundle.usage.task_side_runs == 4
    assert bundle.usage.infrastructure_retry_task_side_runs == 1
    assert bundle.usage.solver_tokens == 43
    assert bundle.usage.cost == "0.43"
    assert bundle.usage.wall_time_seconds == 9


def test_runner_marks_unrecovered_infrastructure_failure_partial():
    async def execute(side, _candidate, _entry, trial_id):
        if side == "candidate":
            return TrialExecution.infrastructure_failure(trial_id, side, "PROVIDER_OUTAGE")
        return TrialExecution.success(trial_id, side, {"success": 0.5}, _ref("evaluation", "8" * 64), _ref("trace", "9" * 64))

    runner = PairedExperimentRunner(execute, objectives=(ObjectiveSpec("success", ObjectiveDirection.MAXIMIZE),), infrastructure_retries=0)
    task_set = TaskSetRef("tasks", ExperimentPhase.DISCOVERY, _ref("taskset", "a" * 64), "b" * 64)
    plan = TrialPlan((TrialEntry("task-a", 1, 0),))

    bundle = asyncio.run(runner.evaluate(_candidate(), task_set, plan, experiment_id="exp-partial")).unwrap()

    assert bundle.status is ExperimentStatus.PARTIAL
    assert bundle.metrics == ()
    assert bundle.failures[0].kind == "infrastructure"


def test_runner_reuses_baseline_only_for_exact_frozen_conditions():
    baseline_calls = 0

    async def execute(side, _candidate, _entry, trial_id):
        nonlocal baseline_calls
        baseline_calls += side == "baseline"
        value = 0.5 if side == "baseline" else 0.7
        return TrialExecution.success(
            trial_id,
            side,
            {"success": value},
            _ref("evaluation", ("c" if side == "baseline" else "d") * 64),
            _ref("trace", ("e" if side == "baseline" else "f") * 64),
        )

    cache = BaselineRunCache()
    runner = PairedExperimentRunner(
        execute,
        objectives=(ObjectiveSpec("success", ObjectiveDirection.MAXIMIZE),),
        baseline_cache=cache,
    )
    task_set = TaskSetRef("tasks", ExperimentPhase.DISCOVERY, _ref("taskset", "6" * 64), "7" * 64)
    plan = TrialPlan((TrialEntry("task-a", 1, 0),))

    asyncio.run(runner.evaluate(_candidate(), task_set, plan, experiment_id="exp-first")).unwrap()
    asyncio.run(
        runner.evaluate(
            replace(_candidate(), candidate_id="other", artifact=_ref("candidate", "0" * 64)),
            task_set,
            plan,
            experiment_id="exp-second",
        )
    ).unwrap()
    changed = replace(_candidate(), solver_digest="0" * 64)
    asyncio.run(runner.evaluate(changed, task_set, plan, experiment_id="exp-changed")).unwrap()

    assert baseline_calls == 2
