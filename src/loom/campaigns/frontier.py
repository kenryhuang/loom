"""Conservative interval Pareto frontier construction."""

from __future__ import annotations

from dataclasses import dataclass

from loom.campaigns.contracts import (
    BudgetUsage,
    DominanceEdge,
    FrontierExclusion,
    FrontierSnapshot,
    GateResult,
    ObjectiveDirection,
    ObjectiveSpec,
)
from loom.campaigns.experiments import objective_gate_result
from loom.campaigns.serialization import canonical_digest, prefixed_id_from_digest, utc_now
from loom.evaluation.experiments import PairedMetricResult


@dataclass(frozen=True, slots=True)
class CandidateObjectives:
    candidate_id: str
    metrics: tuple[PairedMetricResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", tuple(self.metrics))


def dominates(left: CandidateObjectives, right: CandidateObjectives, objectives: tuple[ObjectiveSpec, ...]) -> bool:
    left_metrics = {metric.objective_id: metric for metric in left.metrics}
    right_metrics = {metric.objective_id: metric for metric in right.metrics}
    strict = False
    for objective in objectives:
        left_metric = left_metrics.get(objective.id)
        right_metric = right_metrics.get(objective.id)
        if left_metric is None or right_metric is None:
            return False
        left_interval = left_metric.candidate_interval
        right_interval = right_metric.candidate_interval
        if left_interval is None or right_interval is None:
            return False
        if objective.direction is ObjectiveDirection.MAXIMIZE:
            if left_interval[0] < right_interval[1]:
                return False
            strict = strict or left_interval[0] > right_interval[1]
        else:
            if left_interval[1] > right_interval[0]:
                return False
            strict = strict or left_interval[1] < right_interval[0]
    return strict


def build_frontier_snapshot(
    campaign_id: str,
    iteration: int,
    candidates: tuple[CandidateObjectives, ...],
    objectives: tuple[ObjectiveSpec, ...],
    budget_usage: BudgetUsage,
    *,
    created_at: str | None = None,
) -> FrontierSnapshot:
    eligible: list[CandidateObjectives] = []
    excluded: list[FrontierExclusion] = []
    objective_values: dict[str, dict[str, float | None]] = {}
    for candidate in sorted(candidates, key=lambda item: item.candidate_id):
        metric_by_id = {metric.objective_id: metric for metric in candidate.metrics}
        gates = tuple(
            objective_gate_result(objective, metric_by_id[objective.id]) if objective.id in metric_by_id else GateResult.INSUFFICIENT_EVIDENCE
            for objective in objectives
            if objective.hard
        )
        objective_values[candidate.candidate_id] = {
            objective.id: None if metric_by_id.get(objective.id) is None else metric_by_id[objective.id].candidate_value for objective in objectives
        }
        if any(gate is not GateResult.PASSED for gate in gates):
            excluded.append(FrontierExclusion(candidate.candidate_id, "hard_constraint", gates))
        else:
            eligible.append(candidate)
    edges = tuple(
        DominanceEdge(left.candidate_id, right.candidate_id)
        for left in eligible
        for right in eligible
        if left.candidate_id != right.candidate_id and dominates(left, right, objectives)
    )
    dominated_ids = {edge.dominated_id for edge in edges}
    frontier_ids = tuple(sorted(candidate.candidate_id for candidate in eligible if candidate.candidate_id not in dominated_ids))
    identity = {
        "campaign_id": campaign_id,
        "iteration": iteration,
        "candidate_ids": frontier_ids,
        "objective_values": objective_values,
        "dominance_edges": tuple(sorted(edges, key=lambda item: (item.dominator_id, item.dominated_id))),
        "excluded": tuple(sorted(excluded, key=lambda item: item.candidate_id)),
        "budget_usage": budget_usage,
    }
    return FrontierSnapshot(
        "loom.frontier.snapshot.v1",
        prefixed_id_from_digest("frontier_", canonical_digest(identity)),
        campaign_id,
        created_at or utc_now(),
        iteration,
        frontier_ids,
        objective_values,
        tuple(sorted(edges, key=lambda item: (item.dominator_id, item.dominated_id))),
        tuple(sorted(excluded, key=lambda item: item.candidate_id)),
        budget_usage,
    )


__all__ = ["CandidateObjectives", "build_frontier_snapshot", "dominates"]
