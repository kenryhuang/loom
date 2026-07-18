from __future__ import annotations

import inspect

from loom.campaigns.contracts import ArtifactRef, ExperimentPhase, ExperimentStatus, TaskSetRef
from loom.evaluation import experiments
from loom.evaluation.experiments import ExperimentBundle, ExperimentUsage, TrialEntry, TrialPlan


def test_experiment_contracts_are_neutral_and_deeply_immutable():
    source = inspect.getsource(experiments)
    assert "loom.campaigns" not in source
    task_ref = TaskSetRef(
        "tasks-discovery",
        ExperimentPhase.DISCOVERY,
        ArtifactRef("task.v1", "task_set", "tasks.json", "a" * 64, 1),
        "b" * 64,
    )
    plan = TrialPlan((TrialEntry("task-a", 1, 0), TrialEntry("task-b", 1, 0)))
    bundle = ExperimentBundle(
        schema_version="loom.experiment.bundle.v1",
        experiment_id="exp_test",
        campaign_id="cmp_test",
        candidate_id="cand_test",
        created_at="2026-07-18T00:00:00.000000Z",
        baseline=ArtifactRef("baseline.v1", "baseline", "baseline.json", "c" * 64, 1),
        phase=ExperimentPhase.DISCOVERY,
        task_set=task_ref,
        environment_digest="d" * 64,
        solver_digest="e" * 64,
        tools_digest="f" * 64,
        permissions_digest="1" * 64,
        evaluator_digest="2" * 64,
        trial_plan=plan,
        baseline_evaluations=(),
        candidate_evaluations=(),
        baseline_runs=(),
        candidate_runs=(),
        metrics=(),
        failures=(),
        status=ExperimentStatus.COMPLETED,
        usage=ExperimentUsage(0, 0, 0, "0", 0),
    )

    assert bundle.trial_plan.entries[0].task_fingerprint == "task-a"
    assert isinstance(bundle.trial_plan.entries, tuple)
