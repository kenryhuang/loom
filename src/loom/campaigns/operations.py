"""Idempotent campaign operation and reconciliation contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from loom.campaigns.contracts import ArtifactRef, ExperimentPhase
from loom.campaigns.serialization import canonical_json_bytes
from loom.core import ActorAssertion, FrozenDict, freeze_json


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    phase: ExperimentPhase
    iterations: int = 0
    candidates: int = 0
    candidate_experiments: int = 0
    task_side_runs: int = 0
    infrastructure_retry_task_side_runs: int = 0
    proposer_tokens: int = 0
    solver_tokens: int = 0
    cost: str = "0"
    wall_time_seconds: int = 0

    def __post_init__(self) -> None:
        values = (
            self.candidate_experiments,
            self.task_side_runs,
            self.infrastructure_retry_task_side_runs,
            self.proposer_tokens,
            self.solver_tokens,
            self.iterations,
            self.candidates,
            self.wall_time_seconds,
        )
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("Budget reservation values must be non-negative integers")
        try:
            cost = Decimal(self.cost)
        except InvalidOperation as exc:
            raise ValueError("Budget reservation cost must be a decimal") from exc
        if not cost.is_finite() or cost < 0:
            raise ValueError("Budget reservation cost must be finite and non-negative")
        object.__setattr__(self, "cost", format(cost, "f"))


@dataclass(frozen=True, slots=True)
class CampaignOperation:
    operation_id: str
    campaign_id: str
    input_digest: str
    event_type: str
    actor: ActorAssertion | None = None
    payload: FrozenDict = field(default_factory=FrozenDict)
    output_refs: tuple[ArtifactRef, ...] = ()
    reservation: BudgetReservation | None = None
    complete: bool = True
    lease_seconds: int = 60

    def __post_init__(self) -> None:
        frozen = freeze_json(json.loads(canonical_json_bytes(self.payload)))
        if not isinstance(frozen, FrozenDict):
            raise TypeError("Campaign operation payload must be a mapping")
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "output_refs", tuple(self.output_refs))
        if self.lease_seconds < 0:
            raise ValueError("lease_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class CommittedOperation:
    operation_id: str
    campaign_id: str
    aggregate_version: int
    event_type: str
    event_hash: str
    lease_id: str | None = None
    output_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_refs", tuple(self.output_refs))


@dataclass(frozen=True, slots=True)
class Reconciliation:
    lease_id: str
    operation_id: str
    action: str


@dataclass(frozen=True, slots=True)
class StoreBudgetUsage:
    consumed_candidate_experiments: Mapping[str, int]
    reserved_candidate_experiments: Mapping[str, int]
    consumed_task_side_runs: Mapping[str, int]
    reserved_task_side_runs: Mapping[str, int]
    infrastructure_retry_task_side_runs: int = 0
    reserved_infrastructure_retry_task_side_runs: int = 0
    proposer_tokens: int = 0
    reserved_proposer_tokens: int = 0
    solver_tokens: int = 0
    reserved_solver_tokens: int = 0
    cost: str = "0"
    reserved_cost: str = "0"
    iterations: int = 0
    reserved_iterations: int = 0
    candidates: int = 0
    reserved_candidates: int = 0
    wall_time_seconds: int = 0
    reserved_wall_time_seconds: int = 0


__all__ = [
    "BudgetReservation",
    "CampaignOperation",
    "CommittedOperation",
    "Reconciliation",
    "StoreBudgetUsage",
]
