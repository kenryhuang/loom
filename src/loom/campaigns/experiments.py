"""Paired baseline/candidate experiment execution and aggregation."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import random
import statistics
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Any

from loom.campaigns.contracts import (
    ArtifactRef,
    ExperimentPhase,
    ExperimentStatus,
    GateResult,
    ObjectiveDirection,
    ObjectiveSpec,
    TaskSetRef,
)
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, utc_now
from loom.campaigns.task_sets import fingerprint_task_rows, load_task_manifest
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok
from loom.evaluation.experiments import (
    ExperimentBundle,
    ExperimentFailure,
    ExperimentUsage,
    PairedMetricResult,
    RunArtifactRef,
    TrialEntry,
    TrialPlan,
)


@dataclass(frozen=True, slots=True)
class ValidatedCandidate:
    campaign_id: str
    candidate_id: str
    artifact: ArtifactRef
    baseline: ArtifactRef
    environment_digest: str
    solver_digest: str
    tools_digest: str
    permissions_digest: str
    evaluator_digest: str


@dataclass(frozen=True, slots=True)
class TrialExecution:
    trial_id: str
    side: str
    metrics: FrozenDict = field(default_factory=FrozenDict)
    evaluation_ref: ArtifactRef | None = None
    trace_ref: ArtifactRef | None = None
    failure_kind: str | None = None
    failure_code: str | None = None
    task_side_runs: int = 1
    infrastructure_retry_task_side_runs: int = 0
    solver_tokens: int = 0
    cost: str = "0"
    wall_time_seconds: int = 0

    def __post_init__(self) -> None:
        frozen = freeze_json(self.metrics)
        if not isinstance(frozen, FrozenDict):
            raise TypeError("Trial metrics must be a mapping")
        object.__setattr__(self, "metrics", frozen)
        ExperimentUsage(
            self.task_side_runs,
            self.infrastructure_retry_task_side_runs,
            self.solver_tokens,
            self.cost,
            self.wall_time_seconds,
        )

    @classmethod
    def success(
        cls,
        trial_id: str,
        side: str,
        metrics: Mapping[str, float],
        evaluation_ref: ArtifactRef,
        trace_ref: ArtifactRef,
        *,
        solver_tokens: int = 0,
        cost: str = "0",
        wall_time_seconds: int = 0,
    ):
        return cls(
            trial_id,
            side,
            metrics,
            evaluation_ref,
            trace_ref,
            solver_tokens=solver_tokens,
            cost=cost,
            wall_time_seconds=wall_time_seconds,
        )

    @classmethod
    def infrastructure_failure(
        cls,
        trial_id: str,
        side: str,
        code: str,
        *,
        solver_tokens: int = 0,
        cost: str = "0",
        wall_time_seconds: int = 0,
    ):
        return cls(
            trial_id,
            side,
            failure_kind="infrastructure",
            failure_code=code,
            solver_tokens=solver_tokens,
            cost=cost,
            wall_time_seconds=wall_time_seconds,
        )


@dataclass(frozen=True, slots=True)
class BaselineRunCacheEntry:
    execution: TrialExecution
    source_experiment_id: str

    def __post_init__(self) -> None:
        if not self.source_experiment_id:
            raise ValueError("Baseline cache source experiment ID is required")


class BaselineRunCache:
    """In-memory adapter for exact-condition baseline reuse.

    Durable deployments can supply an object with the same get/put methods;
    the cache key includes every frozen execution condition and trial entry.
    """

    def __init__(self) -> None:
        self._items: dict[str, BaselineRunCacheEntry] = {}

    def get(self, key: str) -> BaselineRunCacheEntry | None:
        return self._items.get(key)

    def put(self, key: str, value: TrialExecution, *, source_experiment_id: str) -> None:
        if value.failure_kind is None:
            self._items[key] = BaselineRunCacheEntry(value, source_experiment_id)


class PairedExperimentRunner:
    def __init__(
        self,
        execute: Callable[[str, ValidatedCandidate, TrialEntry, str], TrialExecution | Awaitable[TrialExecution]],
        *,
        objectives: tuple[ObjectiveSpec, ...],
        infrastructure_retries: int = 0,
        bootstrap_resamples: int = 10_000,
        baseline_cache: BaselineRunCache | None = None,
    ):
        self.execute = execute
        self.objectives = tuple(objectives)
        self.infrastructure_retries = infrastructure_retries
        self.bootstrap_resamples = bootstrap_resamples
        self.baseline_cache = baseline_cache

    async def evaluate(
        self,
        candidate: ValidatedCandidate,
        task_set: TaskSetRef,
        trial_plan: TrialPlan,
        *,
        experiment_id: str,
    ) -> Result:
        baseline_runs: list[RunArtifactRef] = []
        candidate_runs: list[RunArtifactRef] = []
        baseline_evaluations: list[ArtifactRef] = []
        candidate_evaluations: list[ArtifactRef] = []
        failures: list[ExperimentFailure] = []
        usage_task_side_runs = 0
        usage_retry_task_side_runs = 0
        usage_solver_tokens = 0
        usage_cost = Decimal("0")
        usage_wall_time_seconds = 0
        paired_values: dict[str, list[tuple[float | None, float | None]]] = {objective.id: [] for objective in self.objectives}

        for entry in trial_plan.entries:
            trial_id = f"{experiment_id}:{entry.task_fingerprint}:{entry.seed}:{entry.repetition}"
            order = ("baseline", "candidate") if _counterbalance(experiment_id, trial_id) else ("candidate", "baseline")
            results: dict[str, TrialExecution] = {}
            for side in order:
                cache_key = _baseline_cache_key(candidate, task_set, entry)
                cached = None if side != "baseline" or self.baseline_cache is None else self.baseline_cache.get(cache_key)
                result = None if cached is None else cached.execution
                charge_usage = cached is None or cached.source_experiment_id == experiment_id
                if result is None:
                    result = await self._execute_with_retry(side, candidate, entry, trial_id)
                    if side == "baseline" and self.baseline_cache is not None:
                        self.baseline_cache.put(cache_key, result, source_experiment_id=experiment_id)
                if charge_usage:
                    # A same-experiment resume restores costs that were incurred
                    # before its bundle was published. Cross-experiment hits are
                    # already accounted by their source experiment.
                    usage_task_side_runs += result.task_side_runs
                    usage_retry_task_side_runs += result.infrastructure_retry_task_side_runs
                    usage_solver_tokens += result.solver_tokens
                    usage_cost += Decimal(result.cost)
                    usage_wall_time_seconds += result.wall_time_seconds
                results[side] = result
                if result.failure_kind is not None:
                    failures.append(ExperimentFailure(trial_id, side, result.failure_kind, result.failure_code or "UNKNOWN", False, result.failure_code or ""))
                    continue
                assert result.evaluation_ref is not None and result.trace_ref is not None
                run_ref = RunArtifactRef(
                    trial_id,
                    f"{trial_id}:{side}",
                    result.trace_ref,
                    canonical_digest(
                        {
                            "trial": entry,
                            "side": side,
                            "candidate": None if side == "baseline" else candidate.artifact.sha256,
                            "environment": candidate.environment_digest,
                            "solver": candidate.solver_digest,
                            "tools": candidate.tools_digest,
                            "permissions": candidate.permissions_digest,
                            "evaluator": candidate.evaluator_digest,
                        }
                    ),
                    "completed",
                )
                if side == "baseline":
                    baseline_runs.append(run_ref)
                    baseline_evaluations.append(result.evaluation_ref)
                else:
                    candidate_runs.append(run_ref)
                    candidate_evaluations.append(result.evaluation_ref)
            if any(result.failure_kind == "infrastructure" for result in results.values()):
                continue
            for objective in self.objectives:
                paired_values[objective.id].append(
                    (
                        _finite_or_none(results["baseline"].metrics.get(objective.id)),
                        _finite_or_none(results["candidate"].metrics.get(objective.id)),
                    )
                )

        status = ExperimentStatus.PARTIAL if any(item.kind == "infrastructure" for item in failures) else ExperimentStatus.COMPLETED
        metrics = ()
        if status is ExperimentStatus.COMPLETED:
            metrics = tuple(
                aggregate_paired_metric(
                    objective.id,
                    tuple(pair[0] for pair in paired_values[objective.id]),
                    tuple(pair[1] for pair in paired_values[objective.id]),
                    experiment_id=experiment_id,
                    resamples=self.bootstrap_resamples,
                    confidence_level=objective.confidence_level,
                )
                for objective in self.objectives
            )
        return ok(
            ExperimentBundle(
                "loom.experiment.bundle.v1",
                experiment_id,
                candidate.campaign_id,
                candidate.candidate_id,
                utc_now(),
                candidate.baseline,
                task_set.role,
                task_set,
                candidate.environment_digest,
                candidate.solver_digest,
                candidate.tools_digest,
                candidate.permissions_digest,
                candidate.evaluator_digest,
                trial_plan,
                tuple(baseline_evaluations),
                tuple(candidate_evaluations),
                tuple(baseline_runs),
                tuple(candidate_runs),
                metrics,
                tuple(failures),
                status,
                ExperimentUsage(
                    usage_task_side_runs,
                    usage_retry_task_side_runs,
                    usage_solver_tokens,
                    format(usage_cost, "f"),
                    usage_wall_time_seconds,
                ),
            )
        )

    async def _execute_with_retry(self, side: str, candidate: ValidatedCandidate, entry: TrialEntry, trial_id: str) -> TrialExecution:
        solver_tokens = 0
        cost = Decimal("0")
        wall_time_seconds = 0
        for attempt in range(self.infrastructure_retries + 1):
            value = self.execute(side, candidate, entry, trial_id)
            result = await value if inspect.isawaitable(value) else value
            solver_tokens += result.solver_tokens
            cost += Decimal(result.cost)
            wall_time_seconds += result.wall_time_seconds
            if result.failure_kind != "infrastructure" or attempt == self.infrastructure_retries:
                return replace(
                    result,
                    task_side_runs=1,
                    infrastructure_retry_task_side_runs=attempt,
                    solver_tokens=solver_tokens,
                    cost=format(cost, "f"),
                    wall_time_seconds=wall_time_seconds,
                )
        raise AssertionError("unreachable")


def freeze_trial_plan(store, task_set: TaskSetRef, *, repetitions: int = 3) -> Result:
    if repetitions < 1:
        return _invalid_experiment("Trial plan repetitions must be positive")
    resolved = store.artifacts.resolve(task_set.manifest_ref)
    if not resolved.ok:
        return resolved
    rows = load_task_manifest(resolved.value)
    if not rows.ok:
        return rows
    fingerprinted = fingerprint_task_rows(rows.value, task_set.role)
    if not fingerprinted.ok:
        return fingerprinted
    if fingerprinted.value.fingerprint_digest != task_set.fingerprint_digest:
        return _invalid_experiment("Task-set fingerprint does not match its immutable manifest")
    entries = tuple(TrialEntry(canonical_digest(task), repetition, repetition) for task in fingerprinted.value.entries for repetition in range(repetitions))
    return ok(TrialPlan(entries))


def publish_experiment_bundle(store, bundle: ExperimentBundle, *, actor) -> Result:
    authorized = store.identity_provider.authorize(
        actor,
        "campaign_controller",
        forbidden_roles=("campaign_finalizer", "governance_approver", "registry_operator"),
    )
    if not authorized.ok:
        return authorized
    verified = _verify_experiment_refs(store, bundle)
    if not verified.ok:
        return verified
    bundle_payload = json.loads(canonical_json_bytes(bundle))
    payload = {
        "schema_version": "loom.experiment.bundle.v1",
        "bundle": bundle_payload,
        "attestation": {
            "actor": asdict(actor),
            "bundle_digest": canonical_digest(bundle_payload),
        },
    }
    return store.artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="experiment_bundle",
        schema_version="loom.experiment.bundle.v1",
        suffix=".json",
    )


def _verify_experiment_refs(store, bundle: ExperimentBundle) -> Result:
    if bundle.schema_version != "loom.experiment.bundle.v1":
        return _invalid_experiment("Experiment bundle schema version is unsupported")
    trial_count = len(bundle.trial_plan.entries)
    if len(bundle.baseline_evaluations) != len(bundle.baseline_runs) or len(bundle.candidate_evaluations) != len(bundle.candidate_runs):
        return _invalid_experiment("Experiment run and evaluation reference counts must match on each side")
    if bundle.status is ExperimentStatus.COMPLETED and (len(bundle.baseline_runs) != trial_count or len(bundle.candidate_runs) != trial_count):
        return _invalid_experiment("Completed paired experiment evidence must contain one run and evaluation per trial and side")
    if len(bundle.baseline_runs) > trial_count or len(bundle.candidate_runs) > trial_count:
        return _invalid_experiment("Experiment run evidence exceeds the sealed trial plan")
    if bundle.status is ExperimentStatus.COMPLETED and (bundle.failures or not bundle.metrics):
        return _invalid_experiment("Completed experiment evidence cannot contain failures or omit metrics")
    if bundle.usage.task_side_runs > len(bundle.trial_plan.entries) * 2:
        return _invalid_experiment("Experiment usage exceeds the sealed paired trial plan")
    run_ids = tuple(run.run_id for run in (*bundle.baseline_runs, *bundle.candidate_runs))
    if len(set(run_ids)) != len(run_ids):
        return _invalid_experiment("Experiment run identifiers must be unique")
    for run in (*bundle.baseline_runs, *bundle.candidate_runs):
        if run.outcome != "completed" or not run.input_digest:
            return _invalid_experiment("Completed experiment run evidence is malformed")
    for metric in bundle.metrics:
        if metric.valid_pairs < 0 or metric.valid_pairs > min(len(bundle.baseline_runs), len(bundle.candidate_runs)):
            return _invalid_experiment("Experiment metric valid pair count exceeds the sealed trial plan")
        numeric_values = (
            metric.baseline_value,
            metric.candidate_value,
            metric.paired_delta,
            metric.variance,
            *(metric.baseline_interval or ()),
            *(metric.candidate_interval or ()),
            *(metric.delta_interval or ()),
        )
        if any(value is not None and (isinstance(value, bool) or not math.isfinite(float(value))) for value in numeric_values):
            return _invalid_experiment("Experiment metrics must contain only finite numeric values")
    refs = (
        bundle.baseline,
        bundle.task_set.manifest_ref,
        *bundle.baseline_evaluations,
        *bundle.candidate_evaluations,
        *(run.trace for run in bundle.baseline_runs),
        *(run.trace for run in bundle.candidate_runs),
    )
    for ref in refs:
        verified = store.artifacts.read_bytes(ref)
        if not verified.ok:
            return verified
    for ref in (*bundle.baseline_evaluations, *bundle.candidate_evaluations):
        read = store.artifacts.read_bytes(ref, expected_schema="loom.evaluation.bundle.v1")
        if not read.ok:
            return read
        try:
            manifest = json.loads(read.value)
            if manifest.get("schema_version") != "loom.evaluation.bundle.v1" or not isinstance(manifest.get("bundle_id"), str):
                raise ValueError("evaluation bundle identity is missing")
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            return _invalid_experiment("Experiment references a malformed EvaluationBundle", exc=exc)
    return ok(None)


def _invalid_experiment(message: str, *, exc: BaseException | None = None) -> Result:
    return err(
        make_loom_error(
            "VALIDATION_FAILED",
            message,
            retryable=False,
            cause=None if exc is None else {"name": type(exc).__name__, "message": str(exc)},
        )
    )


def load_experiment_bundle(store, ref: ArtifactRef) -> Result:
    read = store.artifacts.read_bytes(ref, expected_schema="loom.experiment.bundle.v1")
    if not read.ok:
        return read
    try:
        envelope = json.loads(read.value)
        if envelope["schema_version"] != "loom.experiment.bundle.v1":
            raise ValueError("experiment envelope schema does not match")
        payload = envelope["bundle"]
        attestation = envelope["attestation"]
        from loom.core import ActorAssertion

        actor = ActorAssertion(**attestation["actor"])
        authorized = store.identity_provider.authorize(
            actor,
            "campaign_controller",
            forbidden_roles=("campaign_finalizer", "governance_approver", "registry_operator"),
        )
        if not authorized.ok:
            return authorized
        if attestation["bundle_digest"] != canonical_digest(payload):
            raise ValueError("experiment attestation digest does not match")
        bundle = ExperimentBundle(
            payload["schema_version"],
            payload["experiment_id"],
            payload["campaign_id"],
            payload["candidate_id"],
            payload["created_at"],
            ArtifactRef(**payload["baseline"]),
            ExperimentPhase(payload["phase"]),
            TaskSetRef(
                payload["task_set"]["task_set_id"],
                ExperimentPhase(payload["task_set"]["role"]),
                ArtifactRef(**payload["task_set"]["manifest_ref"]),
                payload["task_set"]["fingerprint_digest"],
            ),
            payload["environment_digest"],
            payload["solver_digest"],
            payload["tools_digest"],
            payload["permissions_digest"],
            payload["evaluator_digest"],
            TrialPlan(tuple(TrialEntry(**item) for item in payload["trial_plan"]["entries"])),
            tuple(ArtifactRef(**item) for item in payload["baseline_evaluations"]),
            tuple(ArtifactRef(**item) for item in payload["candidate_evaluations"]),
            tuple(_run_ref(item) for item in payload["baseline_runs"]),
            tuple(_run_ref(item) for item in payload["candidate_runs"]),
            tuple(_metric(item) for item in payload["metrics"]),
            tuple(ExperimentFailure(**item) for item in payload["failures"]),
            ExperimentStatus(payload["status"]),
            ExperimentUsage(**payload["usage"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Experiment bundle is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        )
    verified = _verify_experiment_refs(store, bundle)
    return verified if not verified.ok else ok(bundle)


def _run_ref(value) -> RunArtifactRef:
    return RunArtifactRef(value["trial_id"], value["run_id"], ArtifactRef(**value["trace"]), value["input_digest"], value["outcome"])


def _metric(value) -> PairedMetricResult:
    return PairedMetricResult(
        value["objective_id"],
        value["baseline_value"],
        value["candidate_value"],
        value["paired_delta"],
        value["valid_pairs"],
        None if value["baseline_interval"] is None else tuple(value["baseline_interval"]),
        None if value["candidate_interval"] is None else tuple(value["candidate_interval"]),
        None if value["delta_interval"] is None else tuple(value["delta_interval"]),
        value.get("variance"),
    )


def aggregate_paired_metric(
    objective_id: str,
    baseline: tuple[float | None, ...],
    candidate: tuple[float | None, ...],
    *,
    experiment_id: str,
    resamples: int = 10_000,
    confidence_level: float = 0.95,
) -> PairedMetricResult:
    if len(baseline) != len(candidate):
        raise ValueError("Paired metric sides must have equal lengths")
    pairs = tuple(
        (float(left), float(right))
        for left, right in zip(baseline, candidate, strict=True)
        if left is not None and right is not None and math.isfinite(left) and math.isfinite(right)
    )
    if not pairs:
        return PairedMetricResult(objective_id, None, None, None, 0, None, None, None, None)
    left_values = tuple(item[0] for item in pairs)
    right_values = tuple(item[1] for item in pairs)
    deltas = tuple(right - left for left, right in pairs)
    baseline_value = _rounded_mean(left_values)
    candidate_value = _rounded_mean(right_values)
    delta_value = _rounded_mean(deltas)
    variance = 0.0 if len(deltas) == 1 else statistics.variance(deltas)
    if len(pairs) < 5:
        intervals = (None, None, None)
    else:
        seed = int.from_bytes(hashlib.sha256(f"{experiment_id}:{objective_id}".encode()).digest()[:8], "big")
        rng = random.Random(seed)
        left_samples: list[float] = []
        right_samples: list[float] = []
        delta_samples: list[float] = []
        for _ in range(resamples):
            indices = [rng.randrange(len(pairs)) for _ in pairs]
            left_samples.append(statistics.fmean(left_values[index] for index in indices))
            right_samples.append(statistics.fmean(right_values[index] for index in indices))
            delta_samples.append(statistics.fmean(deltas[index] for index in indices))
        intervals = tuple(_percentile_interval(values, confidence_level) for values in (left_samples, right_samples, delta_samples))
    return PairedMetricResult(
        objective_id,
        baseline_value,
        candidate_value,
        delta_value,
        len(pairs),
        intervals[0],
        intervals[1],
        intervals[2],
        variance,
    )


def objective_gate_result(objective: ObjectiveSpec, metric: PairedMetricResult) -> GateResult:
    if not objective.hard:
        return GateResult.PASSED
    if metric.valid_pairs < objective.min_valid_pairs or metric.baseline_interval is None or metric.candidate_interval is None or metric.delta_interval is None:
        return GateResult.INSUFFICIENT_EVIDENCE
    baseline_lower, baseline_upper = metric.baseline_interval
    candidate_lower, candidate_upper = metric.candidate_interval
    delta_lower, delta_upper = metric.delta_interval
    passed = True
    if objective.direction is ObjectiveDirection.MAXIMIZE:
        if objective.absolute_limit is not None:
            passed = passed and candidate_lower >= objective.absolute_limit
        if objective.max_baseline_regression is not None and objective.aggregation == "relative_change":
            passed = passed and candidate_lower >= baseline_upper * (1 - objective.max_baseline_regression)
        elif objective.max_baseline_regression is not None:
            passed = passed and delta_lower >= -objective.max_baseline_regression
    else:
        if objective.absolute_limit is not None:
            passed = passed and candidate_upper <= objective.absolute_limit
        if objective.max_baseline_regression is not None and objective.aggregation == "relative_change":
            passed = passed and candidate_upper <= baseline_lower * (1 + objective.max_baseline_regression)
        elif objective.max_baseline_regression is not None:
            passed = passed and delta_upper <= objective.max_baseline_regression
    return GateResult.PASSED if passed else GateResult.FAILED


def _counterbalance(experiment_id: str, trial_id: str) -> bool:
    return hashlib.sha256(f"{experiment_id}:{trial_id}".encode()).digest()[0] % 2 == 0


def _baseline_cache_key(candidate: ValidatedCandidate, task_set: TaskSetRef, entry: TrialEntry) -> str:
    return canonical_digest(
        {
            "baseline": candidate.baseline,
            "task_set": task_set,
            "trial": entry,
            "environment": candidate.environment_digest,
            "solver": candidate.solver_digest,
            "tools": candidate.tools_digest,
            "permissions": candidate.permissions_digest,
            "evaluator": candidate.evaluator_digest,
        }
    )


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        return None
    return float(value)


def _rounded_mean(values: tuple[float, ...]) -> float:
    return round(statistics.fmean(values), 12)


def _percentile_interval(values: list[float], confidence_level: float) -> tuple[float, float]:
    ordered = sorted(values)
    alpha = (1 - confidence_level) / 2
    lower = ordered[max(0, min(len(ordered) - 1, math.floor(alpha * len(ordered))))]
    upper = ordered[max(0, min(len(ordered) - 1, math.ceil((1 - alpha) * len(ordered)) - 1))]
    return round(lower, 12), round(upper, 12)


__all__ = [
    "PairedExperimentRunner",
    "BaselineRunCache",
    "BaselineRunCacheEntry",
    "TrialExecution",
    "ValidatedCandidate",
    "aggregate_paired_metric",
    "freeze_trial_plan",
    "objective_gate_result",
    "load_experiment_bundle",
    "publish_experiment_bundle",
]
