"""Immutable contracts for one-command optimization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from loom.core import FrozenDict, freeze_json


class OptimizationStage(StrEnum):
    CREATED = "created"
    PREFLIGHT_COMPLETE = "preflight_complete"
    SEED_ANALYSIS_COMPLETE = "seed_analysis_complete"
    CAMPAIGN_INITIALIZED = "campaign_initialized"
    SEARCH_RUNNING = "search_running"
    SEARCH_SEALED = "search_sealed"
    VALIDATION_COMPLETE = "validation_complete"
    FINALISTS_SELECTED = "finalists_selected"
    HOLDOUT_COMPLETE = "holdout_complete"
    GOVERNANCE_COMPLETE = "governance_complete"


class OptimizationLifecycle(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    AWAITING_APPROVAL = "awaiting_approval"


@dataclass(frozen=True, slots=True)
class SplitRatios:
    discovery: Decimal = Decimal("0.60")
    validation: Decimal = Decimal("0.20")
    holdout: Decimal = Decimal("0.20")


@dataclass(frozen=True, slots=True)
class SearchConfig:
    iterations: int = 4
    candidates_per_iteration: int = 2
    candidate_kinds: tuple[str, ...] = ("declarative_patch",)
    editable_surfaces: tuple[str, ...] = (
        "agent.system_prompt",
        "agent.tool_policy",
        "agent.loop_policy",
        "models.solver.request_options",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_kinds", tuple(self.candidate_kinds))
        object.__setattr__(self, "editable_surfaces", tuple(self.editable_surfaces))


@dataclass(frozen=True, slots=True)
class TaskSetConfig:
    split: SplitRatios = field(default_factory=SplitRatios)
    seed: int = 42
    repetitions: int = 3
    minimum_pairs: int = 5
    contamination_threshold: float = 0.80


@dataclass(frozen=True, slots=True)
class ObjectiveConfig:
    primary: str = "task_success_rate"
    max_regression_rate: float = 0.05
    max_cost_increase_ratio: float = 0.25
    max_latency_increase_ratio: float = 0.25


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    max_candidates: int = 8
    max_llm_calls: int = 200
    max_wall_clock_minutes: int = 180
    max_cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    max_parallel_trials: int = 2
    trial_timeout_seconds: int = 900
    verifier_timeout_seconds: int = 120
    stop_on_infrastructure_error: bool = True


@dataclass(frozen=True, slots=True)
class GovernanceConfig:
    mode: str = "local"
    auto_promote_max_risk: str = "low"
    require_approval_for: tuple[str, ...] = ("medium", "high", "executable")

    def __post_init__(self) -> None:
        object.__setattr__(self, "require_approval_for", tuple(self.require_approval_for))


@dataclass(frozen=True, slots=True)
class MetaHarnessConfig:
    proposer_model: str
    solver_model: str
    judge_model: str
    search: SearchConfig = field(default_factory=SearchConfig)
    tasks: TaskSetConfig = field(default_factory=TaskSetConfig)
    objectives: ObjectiveConfig = field(default_factory=ObjectiveConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    seed_analysis_version: str = "v1"


@dataclass(frozen=True, slots=True)
class LoadedOptimizeConfig:
    path: Path
    task_config: Any
    meta: MetaHarnessConfig

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class OptimizationSpec:
    schema_version: str
    optimization_id: str
    optimization_key: str
    trace_path: Path
    config_digest: str
    task_set_digests: Mapping[str, str]
    model_digests: Mapping[str, str]
    campaign_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_path", Path(self.trace_path))
        for name in ("task_set_digests", "model_digests"):
            value = freeze_json(getattr(self, name))
            if not isinstance(value, FrozenDict):
                raise TypeError(f"{name} must be a mapping")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class OptimizationState:
    optimization_id: str
    lifecycle: OptimizationLifecycle
    stage: OptimizationStage
    aggregate_version: int
    completed_operations: tuple[str, ...] = ()
    outputs: Mapping[str, Any] = field(default_factory=FrozenDict)
    failure: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_operations", tuple(self.completed_operations))
        outputs = freeze_json(self.outputs)
        if not isinstance(outputs, FrozenDict):
            raise TypeError("outputs must be a mapping")
        object.__setattr__(self, "outputs", outputs)
        if self.failure is not None:
            failure = freeze_json(self.failure)
            if not isinstance(failure, FrozenDict):
                raise TypeError("failure must be a mapping")
            object.__setattr__(self, "failure", failure)


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    schema_version: str
    optimization_id: str
    campaign_id: str | None
    disposition: str
    selected_candidate_id: str | None
    promotion_decision_ref: Mapping[str, Any] | None
    monitor_ref: Mapping[str, Any] | None
    report_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_path", Path(self.report_path))
        for name in ("promotion_decision_ref", "monitor_ref"):
            value = getattr(self, name)
            if value is not None:
                frozen = freeze_json(value)
                if not isinstance(frozen, FrozenDict):
                    raise TypeError(f"{name} must be a mapping")
                object.__setattr__(self, name, frozen)


__all__ = [
    "BudgetConfig",
    "ExecutionConfig",
    "GovernanceConfig",
    "LoadedOptimizeConfig",
    "MetaHarnessConfig",
    "ObjectiveConfig",
    "OptimizationLifecycle",
    "OptimizationResult",
    "OptimizationSpec",
    "OptimizationStage",
    "OptimizationState",
    "SearchConfig",
    "SplitRatios",
    "TaskSetConfig",
]
