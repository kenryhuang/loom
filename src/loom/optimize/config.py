"""Strict loading of the ``meta_harness`` section in Loom config."""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.optimize.contracts import (
    BudgetConfig,
    ExecutionConfig,
    GovernanceConfig,
    LoadedOptimizeConfig,
    MetaHarnessConfig,
    ObjectiveConfig,
    SearchConfig,
    SplitRatios,
    TaskSetConfig,
)
from loom.tasks.config import load_task_config, parse_yaml_document

_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
_CANDIDATE_KINDS = frozenset({"declarative_patch", "executable_component"})
_APPROVAL_KINDS = frozenset({"medium", "high", "executable"})
_OBJECTIVE_METRICS = frozenset({"task_success_rate", "total_tokens", "wall_time_ms"})
_META_FIELDS = frozenset({"proposer_model", "solver_model", "judge_model", "search", "tasks", "objectives", "budgets", "execution", "governance", "seed_analysis_version"})


class _OptimizeConfigError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def load_optimize_config(path: str | Path) -> Result:
    source = Path(path)
    task_config = load_task_config(source)
    if not task_config.ok:
        return task_config
    try:
        text = source.read_text(encoding="utf-8")
        if source.suffix.lower() in {".yaml", ".yml"}:
            parsed = parse_yaml_document(text, source)
            if not parsed.ok:
                return parsed
            payload = parsed.value
        else:
            payload = tomllib.loads(text)
        root = _mapping(payload, "config")
        meta_value = root.get("meta_harness")
        meta = _parse_meta(_mapping(meta_value, "meta_harness"), task_config.value.models)
        _validate_model_options(task_config.value.models)
        return ok(LoadedOptimizeConfig(source, task_config.value, meta))
    except (OSError, tomllib.TOMLDecodeError, _OptimizeConfigError, InvalidOperation, TypeError, ValueError) as exc:
        field = exc.field if isinstance(exc, _OptimizeConfigError) else "meta_harness"
        return err(
            make_loom_error(
                "OPTIMIZE_CONFIG_INVALID",
                "Meta-harness configuration is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"field": field, "path": str(source)},
            )
        )


def _parse_meta(value: Mapping[str, Any], models: Mapping[str, Any]) -> MetaHarnessConfig:
    _reject_unknown(value, _META_FIELDS, "meta_harness")
    proposer = _required_model(value, "proposer_model", models)
    solver = _required_model(value, "solver_model", models)
    judge = _required_model(value, "judge_model", models)
    search = _parse_search(_optional_mapping(value.get("search"), "meta_harness.search"))
    tasks = _parse_tasks(_optional_mapping(value.get("tasks"), "meta_harness.tasks"))
    objectives = _parse_objectives(_optional_mapping(value.get("objectives"), "meta_harness.objectives"))
    budgets = _parse_budgets(_optional_mapping(value.get("budgets"), "meta_harness.budgets"))
    execution = _parse_execution(_optional_mapping(value.get("execution"), "meta_harness.execution"))
    governance = _parse_governance(_optional_mapping(value.get("governance"), "meta_harness.governance"))
    if search.iterations * search.candidates_per_iteration > budgets.max_candidates:
        raise _OptimizeConfigError("meta_harness.budgets.max_candidates", "candidate search exceeds max_candidates")
    if "executable_component" in search.candidate_kinds and "executable" not in governance.require_approval_for:
        raise _OptimizeConfigError("meta_harness.governance.require_approval_for", "executable candidates require approval")
    version = value.get("seed_analysis_version", "v1")
    if version not in {"v1", "v3"}:
        raise _OptimizeConfigError("meta_harness.seed_analysis_version", "Expected v1 or v3")
    return MetaHarnessConfig(proposer, solver, judge, search, tasks, objectives, budgets, execution, governance, version)


def _parse_search(value: Mapping[str, Any]) -> SearchConfig:
    allowed = frozenset({"iterations", "candidates_per_iteration", "candidate_kinds", "editable_surfaces"})
    _reject_unknown(value, allowed, "meta_harness.search")
    kinds = _strings(value.get("candidate_kinds", ("declarative_patch",)), "meta_harness.search.candidate_kinds")
    if not kinds or set(kinds) - _CANDIDATE_KINDS:
        raise _OptimizeConfigError("meta_harness.search.candidate_kinds", "candidate kinds are invalid")
    defaults = SearchConfig()
    return SearchConfig(
        _positive_int(value.get("iterations", defaults.iterations), "meta_harness.search.iterations"),
        _positive_int(value.get("candidates_per_iteration", defaults.candidates_per_iteration), "meta_harness.search.candidates_per_iteration"),
        kinds,
        _strings(value.get("editable_surfaces", defaults.editable_surfaces), "meta_harness.search.editable_surfaces"),
    )


def _parse_tasks(value: Mapping[str, Any]) -> TaskSetConfig:
    allowed = frozenset({"split", "seed", "repetitions", "minimum_pairs", "contamination_threshold"})
    _reject_unknown(value, allowed, "meta_harness.tasks")
    split_value = _optional_mapping(value.get("split"), "meta_harness.tasks.split")
    _reject_unknown(split_value, frozenset({"discovery", "validation", "holdout"}), "meta_harness.tasks.split")
    split = SplitRatios(
        _decimal(split_value.get("discovery", "0.60"), "meta_harness.tasks.split.discovery"),
        _decimal(split_value.get("validation", "0.20"), "meta_harness.tasks.split.validation"),
        _decimal(split_value.get("holdout", "0.20"), "meta_harness.tasks.split.holdout"),
    )
    if min(split.discovery, split.validation, split.holdout) <= 0 or sum((split.discovery, split.validation, split.holdout), Decimal("0")) != Decimal("1"):
        raise _OptimizeConfigError("meta_harness.tasks.split", "split ratios must be positive and sum to one")
    threshold = _finite_float(value.get("contamination_threshold", 0.80), "meta_harness.tasks.contamination_threshold")
    if threshold <= 0 or threshold > 0.80:
        raise _OptimizeConfigError("meta_harness.tasks.contamination_threshold", "contamination threshold must be within (0, 0.80]")
    minimum_pairs = _positive_int(value.get("minimum_pairs", 5), "meta_harness.tasks.minimum_pairs")
    if minimum_pairs < 5:
        raise _OptimizeConfigError("meta_harness.tasks.minimum_pairs", "minimum_pairs must be at least five for confidence intervals")
    return TaskSetConfig(
        split,
        _integer(value.get("seed", 42), "meta_harness.tasks.seed"),
        _positive_int(value.get("repetitions", 3), "meta_harness.tasks.repetitions"),
        minimum_pairs,
        threshold,
    )


def _parse_objectives(value: Mapping[str, Any]) -> ObjectiveConfig:
    _reject_unknown(value, frozenset({"primary", "constraints"}), "meta_harness.objectives")
    constraints = _optional_mapping(value.get("constraints"), "meta_harness.objectives.constraints")
    allowed = frozenset({"max_regression_rate", "max_cost_increase_ratio", "max_latency_increase_ratio"})
    _reject_unknown(constraints, allowed, "meta_harness.objectives.constraints")
    primary = value.get("primary", "task_success_rate")
    if not isinstance(primary, str) or primary not in _OBJECTIVE_METRICS:
        raise _OptimizeConfigError("meta_harness.objectives.primary", "primary objective is not emitted by optimize trials")
    return ObjectiveConfig(
        primary,
        _non_negative_float(constraints.get("max_regression_rate", 0.05), "meta_harness.objectives.constraints.max_regression_rate"),
        _non_negative_float(constraints.get("max_cost_increase_ratio", 0.25), "meta_harness.objectives.constraints.max_cost_increase_ratio"),
        _non_negative_float(constraints.get("max_latency_increase_ratio", 0.25), "meta_harness.objectives.constraints.max_latency_increase_ratio"),
    )


def _parse_budgets(value: Mapping[str, Any]) -> BudgetConfig:
    allowed = frozenset({"max_candidates", "max_llm_calls", "max_wall_clock_minutes", "max_cost_usd"})
    _reject_unknown(value, allowed, "meta_harness.budgets")
    raw_cost = value.get("max_cost_usd")
    cost = None if raw_cost is None else _decimal(raw_cost, "meta_harness.budgets.max_cost_usd")
    if cost is not None and cost < 0:
        raise _OptimizeConfigError("meta_harness.budgets.max_cost_usd", "cost must be non-negative")
    return BudgetConfig(
        _positive_int(value.get("max_candidates", 8), "meta_harness.budgets.max_candidates"),
        _positive_int(value.get("max_llm_calls", 200), "meta_harness.budgets.max_llm_calls"),
        _positive_int(value.get("max_wall_clock_minutes", 180), "meta_harness.budgets.max_wall_clock_minutes"),
        cost,
    )


def _parse_execution(value: Mapping[str, Any]) -> ExecutionConfig:
    allowed = frozenset({"max_parallel_trials", "trial_timeout_seconds", "verifier_timeout_seconds", "stop_on_infrastructure_error"})
    _reject_unknown(value, allowed, "meta_harness.execution")
    stop = value.get("stop_on_infrastructure_error", True)
    if not isinstance(stop, bool):
        raise _OptimizeConfigError("meta_harness.execution.stop_on_infrastructure_error", "stop flag must be boolean")
    return ExecutionConfig(
        _positive_int(value.get("max_parallel_trials", 2), "meta_harness.execution.max_parallel_trials"),
        _positive_int(value.get("trial_timeout_seconds", 900), "meta_harness.execution.trial_timeout_seconds"),
        _positive_int(value.get("verifier_timeout_seconds", 120), "meta_harness.execution.verifier_timeout_seconds"),
        stop,
    )


def _parse_governance(value: Mapping[str, Any]) -> GovernanceConfig:
    allowed = frozenset({"mode", "auto_promote_max_risk", "require_approval_for"})
    _reject_unknown(value, allowed, "meta_harness.governance")
    mode = value.get("mode", "local")
    if mode not in {"local", "production"}:
        raise _OptimizeConfigError("meta_harness.governance.mode", "governance mode must be local or production")
    risk = value.get("auto_promote_max_risk", "low")
    if risk != "low":
        raise _OptimizeConfigError("meta_harness.governance.auto_promote_max_risk", "automatic promotion is limited to low risk")
    approvals = _strings(value.get("require_approval_for", ("medium", "high", "executable")), "meta_harness.governance.require_approval_for")
    if set(approvals) - _APPROVAL_KINDS:
        raise _OptimizeConfigError("meta_harness.governance.require_approval_for", "approval kinds are invalid")
    return GovernanceConfig(str(mode), str(risk), approvals)


def _validate_model_options(models: Mapping[str, Any]) -> None:
    for name, model in models.items():
        effort = model.request_options.get("reasoning_effort")
        if effort is not None and effort not in _REASONING_EFFORTS:
            raise _OptimizeConfigError(f"models.{name}.request_options.reasoning_effort", "reasoning_effort is invalid")


def _required_model(value: Mapping[str, Any], name: str, models: Mapping[str, Any]) -> str:
    model = value.get(name)
    if not isinstance(model, str) or model not in models:
        raise _OptimizeConfigError(f"meta_harness.{name}", "referenced model is not configured")
    return model


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _OptimizeConfigError(field, "value must be a mapping")
    return value


def _optional_mapping(value: Any, field: str) -> Mapping[str, Any]:
    return {} if value is None else _mapping(value, field)


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _OptimizeConfigError(f"{field}.{unknown[0]}", "unknown configuration field")


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) and item for item in value):
        raise _OptimizeConfigError(field, "value must be a sequence of non-empty strings")
    return tuple(value)


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _OptimizeConfigError(field, "value must be an integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    parsed = _integer(value, field)
    if parsed < 1:
        raise _OptimizeConfigError(field, "value must be positive")
    return parsed


def _finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _OptimizeConfigError(field, "value must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _OptimizeConfigError(field, "value must be finite")
    return parsed


def _non_negative_float(value: Any, field: str) -> float:
    parsed = _finite_float(value, field)
    if parsed < 0:
        raise _OptimizeConfigError(field, "value must be non-negative")
    return parsed


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise _OptimizeConfigError(field, "value must be decimal")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise _OptimizeConfigError(field, "value must be finite")
    return parsed


__all__ = ["load_optimize_config"]
