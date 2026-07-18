"""Dependency-neutral paired experiment contracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from loom.core import Result
from loom.core.artifacts import ArtifactRef, TaskSetRef
from loom.core.experiments import ExperimentPhase, ExperimentStatus


@dataclass(frozen=True, slots=True)
class TrialEntry:
    task_fingerprint: str
    seed: int
    repetition: int


@dataclass(frozen=True, slots=True)
class TrialPlan:
    entries: tuple[TrialEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if len(set(self.entries)) != len(self.entries):
            raise ValueError("Trial plan entries must be unique")


@dataclass(frozen=True, slots=True)
class RunArtifactRef:
    trial_id: str
    run_id: str
    trace: ArtifactRef
    input_digest: str
    outcome: str


@dataclass(frozen=True, slots=True)
class ExperimentFailure:
    trial_id: str
    side: str
    kind: str
    code: str
    retryable: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PairedMetricResult:
    objective_id: str
    baseline_value: float | None
    candidate_value: float | None
    paired_delta: float | None
    valid_pairs: int
    baseline_interval: tuple[float, float] | None
    candidate_interval: tuple[float, float] | None
    delta_interval: tuple[float, float] | None
    variance: float | None = None


@dataclass(frozen=True, slots=True)
class ExperimentUsage:
    task_side_runs: int
    infrastructure_retry_task_side_runs: int
    solver_tokens: int
    cost: str
    wall_time_seconds: int

    def __post_init__(self) -> None:
        counts = (
            self.task_side_runs,
            self.infrastructure_retry_task_side_runs,
            self.solver_tokens,
            self.wall_time_seconds,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise ValueError("Experiment usage counts must be non-negative integers")
        try:
            cost = Decimal(self.cost)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Experiment usage cost must be a decimal string") from exc
        if not cost.is_finite() or cost < 0:
            raise ValueError("Experiment usage cost must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ExperimentBundle:
    schema_version: str
    experiment_id: str
    campaign_id: str
    candidate_id: str
    created_at: str
    baseline: ArtifactRef
    phase: ExperimentPhase
    task_set: TaskSetRef
    environment_digest: str
    solver_digest: str
    tools_digest: str
    permissions_digest: str
    evaluator_digest: str
    trial_plan: TrialPlan
    baseline_evaluations: tuple[ArtifactRef, ...]
    candidate_evaluations: tuple[ArtifactRef, ...]
    baseline_runs: tuple[RunArtifactRef, ...]
    candidate_runs: tuple[RunArtifactRef, ...]
    metrics: tuple[PairedMetricResult, ...]
    failures: tuple[ExperimentFailure, ...]
    status: ExperimentStatus
    usage: ExperimentUsage

    def __post_init__(self) -> None:
        for name in (
            "baseline_evaluations",
            "candidate_evaluations",
            "baseline_runs",
            "candidate_runs",
            "metrics",
            "failures",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))


class ExperimentRunner(Protocol):
    async def evaluate(self, candidate, task_set: TaskSetRef, trial_plan: TrialPlan) -> Result: ...


__all__ = [
    "ExperimentBundle",
    "ExperimentFailure",
    "ExperimentRunner",
    "ExperimentUsage",
    "PairedMetricResult",
    "RunArtifactRef",
    "TrialEntry",
    "TrialPlan",
]
