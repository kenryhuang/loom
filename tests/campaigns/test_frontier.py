from __future__ import annotations

from loom.campaigns.contracts import BudgetUsage, ObjectiveDirection, ObjectiveSpec
from loom.campaigns.frontier import CandidateObjectives, build_frontier_snapshot, dominates
from loom.evaluation.experiments import PairedMetricResult


def _metric(objective: str, value: float, lower: float, upper: float, delta_lower: float = 0.0, delta_upper: float = 0.0):
    return PairedMetricResult(objective, value, value, 0.0, 5, (lower, upper), (lower, upper), (delta_lower, delta_upper), 0.0)


def test_interval_dominance_requires_no_worse_everywhere_and_strictly_better_once():
    objectives = (
        ObjectiveSpec("success", ObjectiveDirection.MAXIMIZE),
        ObjectiveSpec("tokens", ObjectiveDirection.MINIMIZE),
    )
    better = CandidateObjectives("cand_a", (_metric("success", 0.8, 0.75, 0.85), _metric("tokens", 80, 75, 85)))
    worse = CandidateObjectives("cand_b", (_metric("success", 0.6, 0.55, 0.65), _metric("tokens", 100, 95, 105)))

    assert dominates(better, worse, objectives)
    assert not dominates(worse, better, objectives)


def test_overlapping_intervals_and_missing_soft_values_remain_tied():
    objectives = (ObjectiveSpec("success", ObjectiveDirection.MAXIMIZE),)
    left = CandidateObjectives("cand_a", (_metric("success", 0.7, 0.6, 0.8),))
    overlap = CandidateObjectives("cand_b", (_metric("success", 0.65, 0.55, 0.75),))
    missing = CandidateObjectives("cand_c", (PairedMetricResult("success", None, None, None, 0, None, None, None),))

    assert not dominates(left, overlap, objectives)
    assert not dominates(left, missing, objectives)


def test_frontier_excludes_hard_constraint_failure_and_is_deterministic():
    hard = ObjectiveSpec(
        "success",
        ObjectiveDirection.MAXIMIZE,
        hard=True,
        absolute_limit=0.6,
        min_valid_pairs=5,
        missing_policy="fail_closed",
    )
    candidates = (
        CandidateObjectives("cand_b", (_metric("success", 0.5, 0.45, 0.55),)),
        CandidateObjectives("cand_a", (_metric("success", 0.8, 0.75, 0.85),)),
    )

    first = build_frontier_snapshot("cmp_test", 1, candidates, (hard,), BudgetUsage())
    second = build_frontier_snapshot("cmp_test", 1, tuple(reversed(candidates)), (hard,), BudgetUsage())

    assert first.candidate_ids == ("cand_a",)
    assert first.excluded[0].candidate_id == "cand_b"
    assert first.objective_values == second.objective_values
    assert first.dominance_edges == second.dominance_edges
