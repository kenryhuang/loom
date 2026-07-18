"""Campaign input configuration resolution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from loom.campaigns.contracts import (
    ArtifactRef,
    CampaignBudget,
    CampaignSpec,
    CandidateKind,
    CandidatePolicy,
    EnvironmentSpec,
    EvaluatorSpec,
    ExperimentPhase,
    HistoryVisibilityPolicy,
    MonitoringPolicy,
    ObjectiveDirection,
    ObjectiveSpec,
    PhaseBudget,
    PromotionPolicy,
    ProposerSpec,
    RiskLevel,
    SolverSpec,
    TaskSetRef,
)
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, new_prefixed_id, utc_now
from loom.campaigns.task_sets import TaskManifestRow, fingerprint_task_rows, validate_task_set_isolation
from loom.core import Result, err, make_loom_error, ok
from loom.tasks.config import parse_yaml_document


def load_campaign_spec(
    path: str | Path,
    artifact_store,
    *,
    derived_from: str | None = None,
    ancestor_holdout_digest: str | None = None,
) -> Result:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Campaign configuration is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(config_path)},
            )
        )
    if config_path.suffix.lower() in {".yaml", ".yml"}:
        parsed = parse_yaml_document(text, config_path)
        if not parsed.ok:
            return parsed
        payload = parsed.value
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "Campaign configuration is invalid",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                    metadata={"path": str(config_path)},
                )
            )
    try:
        if not isinstance(payload, Mapping):
            raise TypeError("campaign config root must be an object")
        spec = _resolve_spec(payload, config_path.parent, artifact_store, derived_from)
    except _ConfigResolutionError as exc:
        return exc.result
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Campaign configuration is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(config_path)},
            )
        )
    if ancestor_holdout_digest is not None and spec.holdout_set.fingerprint_digest == ancestor_holdout_digest:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Derived campaign must use a new sealed holdout set",
                retryable=False,
                metadata={"holdout_digest": ancestor_holdout_digest},
            )
        )
    return ok(spec)


def load_resolved_campaign_spec(path: str | Path, artifact_store, expected_ref: ArtifactRef) -> Result:
    """Load the immutable, fully resolved CampaignSpec written at creation."""
    source = Path(path)
    try:
        raw = source.read_bytes()
        authoritative = artifact_store.read_bytes(expected_ref, expected_schema="loom.campaign.spec.v1")
        if not authoritative.ok:
            return authoritative
        if raw != authoritative.value:
            raise ValueError("campaign manifest bytes do not match the immutable creation artifact")
        payload = json.loads(raw)
        spec = CampaignSpec(
            str(payload["schema_version"]),
            str(payload["campaign_id"]),
            str(payload["created_at"]),
            _optional_str(payload.get("derived_from")),
            str(payload["objective"]),
            ArtifactRef(**payload["baseline"]),
            SolverSpec(
                str(payload["solver"]["model_profile"]),
                str(payload["solver"]["model_digest"]),
                payload["solver"].get("generation", {}),
            ),
            ProposerSpec(
                str(payload["proposer"]["adapter"]),
                str(payload["proposer"]["model"]),
                payload["proposer"].get("options", {}),
            ),
            EvaluatorSpec(**payload["evaluator"]),
            EnvironmentSpec(**payload["environment"]),
            str(payload["tools_digest"]),
            str(payload["permissions_digest"]),
            tuple(payload["editable_surfaces"]),
            tuple(payload["forbidden_surfaces"]),
            _resolved_task_set(payload["discovery_set"]),
            _resolved_task_set(payload["validation_set"]),
            _resolved_task_set(payload["holdout_set"]),
            tuple(
                ObjectiveSpec(
                    str(item["id"]),
                    ObjectiveDirection(item["direction"]),
                    str(item["aggregation"]),
                    bool(item["hard"]),
                    _optional_float(item.get("absolute_limit")),
                    _optional_float(item.get("max_baseline_regression")),
                    int(item["min_valid_pairs"]),
                    float(item["confidence_level"]),
                    str(item["missing_policy"]),
                )
                for item in payload["objectives"]
            ),
            CampaignBudget(
                int(payload["budget"]["iterations"]),
                int(payload["budget"]["candidates_per_iteration"]),
                tuple(
                    PhaseBudget(
                        ExperimentPhase(item["phase"]),
                        int(item["max_candidate_experiments"]),
                        int(item["max_task_side_runs"]),
                    )
                    for item in payload["budget"]["phases"]
                ),
                int(payload["budget"]["infrastructure_retry_task_side_runs"]),
                int(payload["budget"]["proposer_tokens"]),
                int(payload["budget"]["solver_tokens"]),
                Decimal(str(payload["budget"]["maximum_cost"])),
                int(payload["budget"]["wall_time_seconds"]),
            ),
            _resolved_candidate_policy(payload["candidate_policy"]),
            HistoryVisibilityPolicy(**payload["history_visibility"]),
            PromotionPolicy(
                RiskLevel(payload["promotion_policy"]["auto_promote_max_risk"]),
                bool(payload["promotion_policy"]["executable_requires_human"]),
                bool(payload["promotion_policy"]["require_holdout"]),
                str(payload["promotion_policy"]["primary_objective"]),
                float(payload["promotion_policy"]["minimum_improvement"]),
            ),
            MonitoringPolicy(**payload["monitoring_policy"]),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Resolved campaign manifest is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(source)},
            )
        )
    refs = (
        spec.baseline,
        spec.discovery_set.manifest_ref,
        spec.validation_set.manifest_ref,
        spec.holdout_set.manifest_ref,
    )
    for ref in refs:
        verified = artifact_store.read_bytes(ref)
        if not verified.ok:
            return verified
    return ok(spec)


def _resolved_task_set(value: Mapping[str, Any]) -> TaskSetRef:
    return TaskSetRef(
        str(value["task_set_id"]),
        ExperimentPhase(value["role"]),
        ArtifactRef(**value["manifest_ref"]),
        str(value["fingerprint_digest"]),
    )


def _resolved_candidate_policy(value: Mapping[str, Any]) -> CandidatePolicy:
    return CandidatePolicy(
        tuple(CandidateKind(item) for item in value["kinds"]),
        tuple(value["editable_surfaces"]),
        tuple(value["forbidden_surfaces"]),
        tuple(value["allowed_patch_operations"]),
        tuple(value["allowed_imports"]),
        int(value["max_source_files"]),
        int(value["max_source_bytes"]),
        int(value["max_ast_nodes"]),
        float(value["duplicate_threshold"]),
        bool(value["executable_enabled"]),
    )


def _resolve_spec(payload: Mapping[str, Any], base_dir: Path, artifact_store, derived_from: str | None) -> CampaignSpec:
    baseline_payload = _mapping(payload["baseline"], "baseline")
    baseline = artifact_store.publish_bytes(
        canonical_json_bytes(baseline_payload),
        kind="baseline",
        schema_version="loom.campaign.baseline.v1",
        suffix=".json",
    ).unwrap()
    solver_payload = _mapping(payload["solver"], "solver")
    proposer_payload = _mapping(payload["proposer"], "proposer")
    evaluator_payload = _mapping(payload["evaluator"], "evaluator")
    environment_payload = _mapping(payload["environment"], "environment")
    editable = _strings(payload.get("editable_surfaces", ()), "editable_surfaces")
    forbidden = _strings(payload.get("forbidden_surfaces", ()), "forbidden_surfaces")
    objectives = tuple(_objective(_mapping(item, "objective")) for item in _sequence(payload["objectives"], "objectives"))
    budget_payload = _mapping(payload["budget"], "budget")
    phases = tuple(
        PhaseBudget(
            ExperimentPhase(str(item["phase"])),
            int(item["max_candidate_experiments"]),
            int(item["max_task_side_runs"]),
        )
        for raw in _sequence(budget_payload["phases"], "budget.phases")
        for item in (_mapping(raw, "phase budget"),)
    )
    promotion = _mapping(payload.get("promotion", {}), "promotion")
    monitoring = _mapping(payload.get("monitoring", {}), "monitoring")
    history_visibility = _mapping(payload.get("history_visibility", {}), "history_visibility")
    discovery_set, discovery_fingerprints = _task_set(payload["discovery_set"], ExperimentPhase.DISCOVERY, base_dir, artifact_store)
    validation_set, validation_fingerprints = _task_set(payload["validation_set"], ExperimentPhase.VALIDATION, base_dir, artifact_store)
    holdout_set, holdout_fingerprints = _task_set(payload["holdout_set"], ExperimentPhase.HOLDOUT, base_dir, artifact_store)
    isolation = validate_task_set_isolation((discovery_fingerprints, validation_fingerprints, holdout_fingerprints))
    if not isolation.ok:
        raise _ConfigResolutionError(isolation)
    return CampaignSpec(
        schema_version=str(payload.get("schema_version", "loom.campaign.spec.v1")),
        campaign_id=new_prefixed_id("cmp_"),
        created_at=utc_now(),
        derived_from=derived_from,
        objective=str(payload["objective"]),
        baseline=baseline,
        solver=SolverSpec(
            str(solver_payload["model_profile"]),
            canonical_digest(solver_payload),
            {key: value for key, value in solver_payload.items() if key != "model_profile"},
        ),
        proposer=ProposerSpec(
            str(proposer_payload["adapter"]),
            str(proposer_payload.get("model", "default")),
            {key: value for key, value in proposer_payload.items() if key not in {"adapter", "model"}},
        ),
        evaluator=EvaluatorSpec(
            str(evaluator_payload["deterministic_profile"]),
            _optional_str(evaluator_payload.get("judge_profile")),
            canonical_digest(evaluator_payload),
        ),
        environment=EnvironmentSpec(
            str(environment_payload["image_digest"]),
            str(environment_payload["runner_version"]),
            str(environment_payload["isolation_profile"]),
        ),
        tools_digest=str(payload["tools_digest"]),
        permissions_digest=str(payload["permissions_digest"]),
        editable_surfaces=editable,
        forbidden_surfaces=forbidden,
        discovery_set=discovery_set,
        validation_set=validation_set,
        holdout_set=holdout_set,
        objectives=objectives,
        budget=CampaignBudget(
            int(budget_payload["iterations"]),
            int(budget_payload["candidates_per_iteration"]),
            phases,
            int(budget_payload["infrastructure_retry_task_side_runs"]),
            int(budget_payload["proposer_tokens"]),
            int(budget_payload["solver_tokens"]),
            Decimal(str(budget_payload["maximum_cost"])),
            int(budget_payload["wall_time_seconds"]),
        ),
        candidate_policy=CandidatePolicy(
            (CandidateKind.DECLARATIVE_PATCH,),
            editable,
            forbidden,
            ("replace", "set", "append_rule", "set_limit"),
        ),
        history_visibility=HistoryVisibilityPolicy(
            _strings(
                history_visibility.get("discovery_fields", ("surface", "message", "details", "findings", "metrics")),
                "history_visibility.discovery_fields",
            ),
            _strings(history_visibility.get("trace_fields", ("event_type", "excerpt")), "history_visibility.trace_fields"),
            bool(history_visibility.get("candidate_artifacts", True)),
            bool(history_visibility.get("imported_experience", True)),
        ),
        promotion_policy=PromotionPolicy(
            RiskLevel(str(promotion.get("auto_promote_max_risk", "low"))),
            bool(promotion.get("executable_requires_human", True)),
            bool(promotion.get("require_holdout", True)),
        ),
        monitoring_policy=MonitoringPolicy(
            int(monitoring.get("ttl_runs", 20)),
            int(monitoring.get("minimum_sample_size", 5)),
            float(monitoring.get("quality_regression_threshold", 0.05)),
            bool(monitoring.get("rollback_on_critical_regression", True)),
        ),
    )


def _task_set(value: Any, role: ExperimentPhase, base_dir: Path, artifact_store):
    source = Path(str(value))
    if not source.is_absolute():
        source = base_dir / source
    content = source.read_bytes()
    rows = []
    for line_number, raw_line in enumerate(content.decode("utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
            if not isinstance(item, Mapping):
                raise TypeError("task row must be an object")
            rows.append(
                TaskManifestRow(
                    str(item["task_id"]),
                    str(item["owner"]),
                    str(item["source_evidence_ref"]),
                    str(item["use_basis"]),
                    str(item["project_snapshot_digest"]),
                    str(item["template_lineage"]),
                    str(item["sanitizer_version"]),
                    str(item["content"]),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid task manifest row {line_number}: {exc}") from exc
    fingerprinted = fingerprint_task_rows(tuple(rows), role)
    if not fingerprinted.ok:
        raise _ConfigResolutionError(fingerprinted)
    manifest_ref = artifact_store.publish_bytes(
        content,
        kind="task_set",
        schema_version="loom.task-set.manifest.v1",
        suffix=source.suffix,
    ).unwrap()
    digest = fingerprinted.value.fingerprint_digest
    return TaskSetRef(f"tasks-{role.value}-{digest[:12]}", role, manifest_ref, digest), fingerprinted.value


class _ConfigResolutionError(Exception):
    def __init__(self, result: Result):
        super().__init__(result.error.message if result.error is not None else "campaign config resolution failed")
        self.result = result


def _objective(value: Mapping[str, Any]) -> ObjectiveSpec:
    hard = bool(value.get("hard", False))
    return ObjectiveSpec(
        id=str(value["id"]),
        direction=ObjectiveDirection(str(value["direction"])),
        aggregation=str(value.get("aggregation", "mean")),
        hard=hard,
        absolute_limit=_optional_float(value.get("absolute_limit")),
        max_baseline_regression=_optional_float(value.get("max_baseline_regression")),
        min_valid_pairs=int(value.get("min_valid_pairs", 1)),
        confidence_level=float(value.get("confidence_level", 0.95)),
        missing_policy=str(value.get("missing_policy", "fail_closed" if hard else "exclude")),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, list | tuple):
        raise TypeError(f"{name} must be a sequence")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    sequence = _sequence(value, name)
    if not all(isinstance(item, str) for item in sequence):
        raise TypeError(f"{name} must contain strings")
    return tuple(sequence)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = ["load_campaign_spec", "load_resolved_campaign_spec"]
