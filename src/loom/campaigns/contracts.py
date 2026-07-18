"""Immutable domain contracts shared by campaigns and governance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from loom.core import FrozenDict, freeze_json
from loom.core.artifacts import ArtifactRef as ArtifactRef
from loom.core.artifacts import TaskSetRef as TaskSetRef
from loom.core.experiments import ExperimentPhase as ExperimentPhase
from loom.core.experiments import ExperimentStatus as ExperimentStatus


class CandidateKind(StrEnum):
    DECLARATIVE_PATCH = "declarative_patch"
    EXECUTABLE_COMPONENT = "executable_component"


class CandidateLifecycle(StrEnum):
    PROPOSED = "proposed"
    INVALID = "invalid"
    VALIDATED = "validated"
    EVALUATED = "evaluated"
    VALIDATION_REJECTED = "validation_rejected"
    VALIDATION_PASSED = "validation_passed"
    HOLDOUT_REJECTED = "holdout_rejected"
    HOLDOUT_PASSED = "holdout_passed"
    AWAITING_APPROVAL = "awaiting_approval"
    PROMOTED = "promoted"
    EXPIRED = "expired"
    ROLLED_BACK = "rolled_back"


class ObjectiveDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FORBIDDEN = "forbidden"


class GateResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REVIEW_REQUIRED = "review_required"


class PromotionDisposition(StrEnum):
    REJECTED = "rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    PROMOTED = "promoted"


class GovernanceAction(StrEnum):
    REVIEWED = "reviewed"
    ACTIVATED = "activated"
    EXPIRED = "expired"
    ROLLED_BACK = "rolled_back"


class CampaignLifecycle(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    SEARCH_SEALED = "search_sealed"
    FINALIZED = "finalized"
    ABORTED = "aborted"
    FROZEN = "frozen"


@dataclass(frozen=True, slots=True)
class MetricPrediction:
    metric_id: str
    expected_delta: float


@dataclass(frozen=True, slots=True)
class CandidateHypothesis:
    problem: str
    mechanism: str
    expected_improvements: tuple[MetricPrediction, ...]
    expected_regressions: tuple[MetricPrediction, ...]
    preserved_behaviors: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_improvements", tuple(self.expected_improvements))
        object.__setattr__(self, "expected_regressions", tuple(self.expected_regressions))
        object.__setattr__(self, "preserved_behaviors", tuple(self.preserved_behaviors))


@dataclass(frozen=True, slots=True)
class ObjectiveSpec:
    id: str
    direction: ObjectiveDirection
    aggregation: str = "mean"
    hard: bool = False
    absolute_limit: float | None = None
    max_baseline_regression: float | None = None
    min_valid_pairs: int = 1
    confidence_level: float = 0.95
    missing_policy: str = "exclude"

    def __post_init__(self) -> None:
        if not self.id or not self.aggregation:
            raise ValueError("Objective id and aggregation are required")
        if self.max_baseline_regression is not None and self.max_baseline_regression < 0:
            raise ValueError("max_baseline_regression must be non-negative")
        if not 0 < self.confidence_level < 1:
            raise ValueError("confidence_level must be between zero and one")
        if self.min_valid_pairs < 1:
            raise ValueError("min_valid_pairs must be positive")
        if self.hard:
            if self.absolute_limit is None and self.max_baseline_regression is None:
                raise ValueError("Hard objective requires an absolute or regression limit")
            if self.min_valid_pairs < 5:
                raise ValueError("Hard objective requires at least five valid pairs")
            if self.missing_policy != "fail_closed":
                raise ValueError("Hard objective missing values must fail closed")


@dataclass(frozen=True, slots=True)
class PhaseBudget:
    phase: ExperimentPhase
    max_candidate_experiments: int
    max_task_side_runs: int

    def __post_init__(self) -> None:
        if self.phase is ExperimentPhase.MONITORING:
            raise ValueError("Monitoring uses MonitoringPolicy budget")
        if self.max_candidate_experiments < 0 or self.max_task_side_runs < 0:
            raise ValueError("Phase budget limits must be non-negative")


@dataclass(frozen=True, slots=True)
class CampaignBudget:
    iterations: int
    candidates_per_iteration: int
    phases: tuple[PhaseBudget, ...]
    infrastructure_retry_task_side_runs: int
    proposer_tokens: int
    solver_tokens: int
    maximum_cost: Decimal
    wall_time_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", tuple(self.phases))
        phase_names = [item.phase for item in self.phases]
        if len(set(phase_names)) != len(phase_names):
            raise ValueError("Campaign budget contains duplicate phase budgets")
        scalar_limits = (
            self.iterations,
            self.candidates_per_iteration,
            self.proposer_tokens,
            self.solver_tokens,
            self.wall_time_seconds,
        )
        if any(value <= 0 for value in scalar_limits):
            raise ValueError("Campaign budget primary limits must be positive")
        if self.infrastructure_retry_task_side_runs < 0 or self.maximum_cost < 0:
            raise ValueError("Campaign budget retry and cost limits must be non-negative")


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    iterations: int = 0
    candidates: int = 0
    candidate_experiments: FrozenDict = field(default_factory=FrozenDict)
    task_side_runs: FrozenDict = field(default_factory=FrozenDict)
    infrastructure_retry_task_side_runs: int = 0
    proposer_tokens: int = 0
    solver_tokens: int = 0
    cost: str = "0"
    wall_time_seconds: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_experiments", _freeze_mapping(self.candidate_experiments))
        object.__setattr__(self, "task_side_runs", _freeze_mapping(self.task_side_runs))


@dataclass(frozen=True, slots=True)
class SolverSpec:
    model_profile: str
    model_digest: str
    generation: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _freeze_mapping(self.generation))


@dataclass(frozen=True, slots=True)
class ProposerSpec:
    adapter: str
    model: str
    options: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _freeze_mapping(self.options))


@dataclass(frozen=True, slots=True)
class EvaluatorSpec:
    deterministic_profile: str
    judge_profile: str | None
    digest: str
    bootstrap_resamples: int = 10_000
    confidence_level: float = 0.95
    missing_policy: str = "fail_closed"


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    image_digest: str
    runner_version: str
    isolation_profile: str


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    kinds: tuple[CandidateKind, ...]
    editable_surfaces: tuple[str, ...]
    forbidden_surfaces: tuple[str, ...]
    allowed_patch_operations: tuple[str, ...]
    allowed_imports: tuple[str, ...] = ()
    max_source_files: int = 0
    max_source_bytes: int = 0
    max_ast_nodes: int = 0
    duplicate_threshold: float = 1.0
    executable_enabled: bool = False

    def __post_init__(self) -> None:
        for name in ("kinds", "editable_surfaces", "forbidden_surfaces", "allowed_patch_operations", "allowed_imports"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class HistoryVisibilityPolicy:
    discovery_fields: tuple[str, ...]
    trace_fields: tuple[str, ...]
    candidate_artifacts: bool = True
    imported_experience: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "discovery_fields", tuple(self.discovery_fields))
        object.__setattr__(self, "trace_fields", tuple(self.trace_fields))


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    auto_promote_max_risk: RiskLevel = RiskLevel.LOW
    executable_requires_human: bool = True
    require_holdout: bool = True
    primary_objective: str = "task_success"
    minimum_improvement: float = 0.0


@dataclass(frozen=True, slots=True)
class MonitoringPolicy:
    ttl_runs: int
    minimum_sample_size: int
    quality_regression_threshold: float
    rollback_on_critical_regression: bool = True
    heartbeat_grace_seconds: int = 60
    resource_ceilings: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_ceilings", _freeze_mapping(self.resource_ceilings))


@dataclass(frozen=True, slots=True)
class CampaignSpec:
    schema_version: str
    campaign_id: str
    created_at: str
    derived_from: str | None
    objective: str
    baseline: ArtifactRef
    solver: SolverSpec
    proposer: ProposerSpec
    evaluator: EvaluatorSpec
    environment: EnvironmentSpec
    tools_digest: str
    permissions_digest: str
    editable_surfaces: tuple[str, ...]
    forbidden_surfaces: tuple[str, ...]
    discovery_set: TaskSetRef
    validation_set: TaskSetRef
    holdout_set: TaskSetRef
    objectives: tuple[ObjectiveSpec, ...]
    budget: CampaignBudget
    candidate_policy: CandidatePolicy
    history_visibility: HistoryVisibilityPolicy
    promotion_policy: PromotionPolicy
    monitoring_policy: MonitoringPolicy

    def __post_init__(self) -> None:
        for name in ("editable_surfaces", "forbidden_surfaces", "objectives"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    imports: tuple[str, ...]
    dependencies: tuple[str, ...]
    filesystem_read_roots: tuple[str, ...]
    filesystem_write_roots: tuple[str, ...]
    callable_tools: tuple[str, ...]
    input_schema: FrozenDict
    output_schema: FrozenDict
    resource_limits: FrozenDict
    subprocess_required: bool = False
    network_required: bool = False

    def __post_init__(self) -> None:
        for name in ("imports", "dependencies", "filesystem_read_roots", "filesystem_write_roots", "callable_tools"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("input_schema", "output_schema", "resource_limits"):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    kind: CandidateKind
    parent_ids: tuple[str, ...]
    inspiration_ids: tuple[str, ...]
    hypothesis: CandidateHypothesis
    evidence_refs: tuple[str, ...]
    changed_surfaces: tuple[str, ...]
    artifact_path: str
    patch_path: str | None
    capability_manifest: CapabilityManifest

    def __post_init__(self) -> None:
        for name in ("parent_ids", "inspiration_ids", "evidence_refs", "changed_surfaces"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class CandidateBundle:
    schema_version: str
    candidate_id: str
    campaign_id: str
    created_at: str
    kind: CandidateKind
    parent_ids: tuple[str, ...]
    inspiration_ids: tuple[str, ...]
    proposer_session: ArtifactRef
    hypothesis: CandidateHypothesis
    evidence_refs: tuple[str, ...]
    changed_surfaces: tuple[str, ...]
    artifact: ArtifactRef
    patch: ArtifactRef | None
    capability_manifest: CapabilityManifest

    def __post_init__(self) -> None:
        for name in ("parent_ids", "inspiration_ids", "evidence_refs", "changed_surfaces"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class CandidateState:
    candidate_id: str
    aggregate_version: int
    lifecycle: CandidateLifecycle
    validation_refs: tuple[ArtifactRef, ...] = ()
    experiment_refs: tuple[ArtifactRef, ...] = ()
    risk_ref: ArtifactRef | None = None
    latest_governance_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "validation_refs", tuple(self.validation_refs))
        object.__setattr__(self, "experiment_refs", tuple(self.experiment_refs))


@dataclass(frozen=True, slots=True)
class CampaignEvent:
    schema_version: str
    event_id: str
    campaign_id: str
    sequence: int
    aggregate_version: int
    event_type: str
    created_at: str
    operation_id: str
    payload: FrozenDict
    previous_hash: str | None
    event_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class DominanceEdge:
    dominator_id: str
    dominated_id: str


@dataclass(frozen=True, slots=True)
class FrontierExclusion:
    candidate_id: str
    reason: str
    gate_results: tuple[GateResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_results", tuple(self.gate_results))


@dataclass(frozen=True, slots=True)
class FrontierSnapshot:
    schema_version: str
    snapshot_id: str
    campaign_id: str
    created_at: str
    iteration: int
    candidate_ids: tuple[str, ...]
    objective_values: FrozenDict
    dominance_edges: tuple[DominanceEdge, ...]
    excluded: tuple[FrontierExclusion, ...]
    budget_usage: BudgetUsage

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))
        object.__setattr__(self, "objective_values", _freeze_mapping(self.objective_values))
        object.__setattr__(self, "dominance_edges", tuple(self.dominance_edges))
        object.__setattr__(self, "excluded", tuple(self.excluded))


@dataclass(frozen=True, slots=True)
class OperationLease:
    lease_id: str
    operation_id: str
    aggregate_id: str
    input_digest: str
    expected_version: int
    expires_at: str
    status: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    gate_id: str
    mandatory: bool
    result: GateResult
    evidence_refs: tuple[ArtifactRef, ...]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    schema_version: str
    candidate_id: str
    level: RiskLevel
    matched_rules: tuple[str, ...]
    rule_set: ArtifactRef
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "matched_rules", tuple(self.matched_rules))


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    subject: str
    role: str
    created_at: str
    expires_at: str
    candidate_digest: str
    baseline_digest: str
    policy_digest: str
    risk_rules_digest: str
    gate_digest: str
    decision: str
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    schema_version: str
    decision_id: str
    campaign_id: str
    candidate_id: str
    created_at: str
    baseline_digest: str
    policy_digest: str
    risk_rule_set: ArtifactRef
    gates: tuple[GateDecision, ...]
    computed_risk: RiskLevel
    matched_risk_rules: tuple[str, ...]
    human_approval: ApprovalRecord | None
    decision: PromotionDisposition
    active_artifact: ArtifactRef | None
    rollback_artifact: ArtifactRef | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "gates", tuple(self.gates))
        object.__setattr__(self, "matched_risk_rules", tuple(self.matched_risk_rules))


def _freeze_mapping(value: Mapping[str, Any]) -> FrozenDict:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenDict):
        raise TypeError("Expected a mapping")
    return frozen


__all__ = [name for name in globals() if not name.startswith("_") and name not in {"Any", "Decimal", "FrozenDict", "Mapping"}]
