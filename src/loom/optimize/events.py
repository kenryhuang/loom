"""Versioned, holdout-safe observer events for optimization runs."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from loom.campaigns.serialization import canonical_json_bytes, utc_now
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok

_FORBIDDEN_HOLDOUT_KEYS = frozenset(
    {
        "task",
        "tasks",
        "task_prompt",
        "prompt",
        "content",
        "workspace",
        "workspace_path",
        "path",
        "expected_output",
        "expected_outputs",
        "verifier",
        "verifier_data",
        "score",
        "scores",
        "judge_rationale",
        "rationale",
        "error_message",
        "message",
        "cause",
        "stdout",
        "stderr",
    }
)
_HOLDOUT_ALLOWED_SCOPE_KEYS = frozenset({"phase", "candidate_id", "experiment_id"})
_HOLDOUT_ALLOWED_PAYLOAD_KEYS = {
    "optimization.holdout.status": frozenset({"state", "finalist_ids", "finalist_count", "evaluated_ids", "evidence_ref_count"}),
    "optimization.experiment.started": frozenset({"trial_count"}),
    "optimization.experiment.completed": frozenset({"status", "task_side_runs", "solver_tokens", "cost", "wall_time_seconds"}),
    "optimization.budget.updated": frozenset({"used", "limits"}),
    "optimization.stage.started": frozenset({"operation_id", "lease_id", "aggregate_version"}),
    "optimization.stage.replayed": frozenset({"operation_id", "lease_id", "aggregate_version", "output_keys"}),
    "optimization.stage.completed": frozenset({"operation_id", "lease_id", "aggregate_version", "output_keys"}),
    "optimization.stage.failed": frozenset({"operation_id", "lease_id", "error_code"}),
    "optimization.pause.requested": frozenset({"operation_id", "lease_id"}),
    "optimization.cancel.requested": frozenset({"operation_id", "lease_id"}),
    "optimization.paused": frozenset({"operation_id", "lease_id", "disposition", "next_action"}),
    "optimization.failed": frozenset({"error_code"}),
    "optimization.snapshot.loaded": frozenset(
        {
            "lifecycle",
            "aggregate_version",
            "operations",
            "completed_stages",
            "budgets",
            "candidates",
            "frontier_ids",
            "budget_used",
            "governance",
            "search_iteration",
            "search_iterations",
        }
    ),
}
_HOLDOUT_STAGE_PROTECTED_EVENT_TYPES = frozenset(
    {
        "optimization.stage.started",
        "optimization.stage.replayed",
        "optimization.stage.completed",
        "optimization.stage.failed",
        "optimization.pause.requested",
        "optimization.cancel.requested",
        "optimization.paused",
        "optimization.failed",
        "optimization.snapshot.loaded",
    }
)


@dataclass(frozen=True, slots=True)
class OptimizationSnapshot:
    optimization_id: str
    campaign_id: str | None
    stage: str = "created"
    lifecycle: str = "running"
    aggregate_version: int = 0
    operations: tuple[Mapping[str, Any], ...] = ()
    budgets: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(_freeze_mapping(value, "snapshot operation") for value in self.operations))
        object.__setattr__(self, "budgets", _freeze_mapping(self.budgets, "snapshot budgets"))


@dataclass(slots=True)
class OptimizationEventEmitter:
    optimization_id: str
    campaign_id: str | None
    observer: Any | None = None
    _sequence: int = 0
    _holdout_warning_emitted: bool = False

    async def emit(
        self,
        event_type: str,
        *,
        stage: str | None = None,
        status: str = "event",
        scope: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Result:
        if not isinstance(event_type, str) or not event_type:
            return _event_error("OPTIMIZATION_EVENT_INVALID", "Optimization observer event type is required")
        next_sequence = self._sequence + 1
        built = build_optimization_event(
            event_type,
            sequence=next_sequence,
            optimization_id=self.optimization_id,
            campaign_id=self.campaign_id,
            stage=stage,
            status=status,
            scope=scope,
            payload=payload,
        )
        if not built.ok:
            if built.error is not None and built.error.code == "HOLDOUT_EVENT_FORBIDDEN" and not self._holdout_warning_emitted:
                warning = build_optimization_event(
                    "optimization.observer.warning",
                    sequence=next_sequence,
                    optimization_id=self.optimization_id,
                    campaign_id=self.campaign_id,
                    stage=None,
                    status="warning",
                    scope={},
                    payload={"code": "HOLDOUT_EVENT_FORBIDDEN"},
                )
                if warning.ok:
                    self._sequence = next_sequence
                    self._holdout_warning_emitted = True
                    await self._notify(warning.value)
            return built
        self._sequence = next_sequence
        await self._notify(built.value)
        return ok(built.value)

    async def _notify(self, event: Mapping[str, Any]) -> None:
        if self.observer is None:
            return
        try:
            emitted = self.observer.emit(event)
            if inspect.isawaitable(emitted):
                await emitted
        except Exception:
            return


class ScopedOptimizationTraceSink:
    def __init__(
        self,
        emitter: OptimizationEventEmitter,
        *,
        stage: str,
        scope: Mapping[str, Any],
    ) -> None:
        self.emitter = emitter
        self.stage = stage
        self.scope = _freeze_mapping(scope, "runtime event scope")

    async def emit(self, event: Mapping[str, Any]) -> Result:
        try:
            runtime_event = json.loads(canonical_json_bytes(event))
        except (TypeError, ValueError):
            return ok(None)
        emitted = await self.emitter.emit(
            "optimization.runtime.event",
            stage=self.stage,
            status=_runtime_status(str(runtime_event.get("type", "unknown"))),
            scope=self.scope,
            payload={"runtime_event": runtime_event},
        )
        if not emitted.ok and emitted.error.code == "HOLDOUT_EVENT_FORBIDDEN":
            return ok(None)
        return ok(None) if emitted.ok else emitted


def build_optimization_event(
    event_type: str,
    *,
    sequence: int,
    optimization_id: str,
    campaign_id: str | None,
    stage: str | None,
    status: str,
    scope: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> Result:
    if sequence < 1 or not optimization_id or not status:
        return _event_error("OPTIMIZATION_EVENT_INVALID", "Optimization observer event envelope is invalid")
    scope_value = {} if scope is None else dict(scope)
    payload_value = {} if payload is None else dict(payload)
    phase = str(scope_value.get("phase", ""))
    protected_holdout = (
        phase == "holdout"
        or event_type.startswith("optimization.holdout")
        or (stage == "holdout_complete" and event_type in _HOLDOUT_STAGE_PROTECTED_EVENT_TYPES)
    )
    if protected_holdout:
        forbidden_scope = set(map(str, scope_value)) - _HOLDOUT_ALLOWED_SCOPE_KEYS
        allowed_payload = _HOLDOUT_ALLOWED_PAYLOAD_KEYS.get(event_type)
        forbidden_payload = set(map(str, payload_value)) - (allowed_payload or frozenset())
        if event_type not in _HOLDOUT_ALLOWED_PAYLOAD_KEYS:
            return _event_error(
                "HOLDOUT_EVENT_FORBIDDEN",
                "Holdout observer event type is not aggregate-safe",
                field=event_type,
            )
        if forbidden_scope or forbidden_payload:
            field = sorted(forbidden_scope or forbidden_payload)[0]
            return _event_error("HOLDOUT_EVENT_FORBIDDEN", "Holdout observer event contains a forbidden field", field=field)
        forbidden = _find_forbidden_key(payload_value)
        if forbidden is not None:
            return _event_error(
                "HOLDOUT_EVENT_FORBIDDEN",
                "Holdout observer event contains forbidden detail",
                field=forbidden,
            )
    try:
        frozen = freeze_json(
            {
                "schema_version": "loom.optimization.event.v1",
                "type": event_type,
                "sequence": sequence,
                "at": utc_now(),
                "optimization_id": optimization_id,
                "campaign_id": campaign_id,
                "stage": stage,
                "status": status,
                "scope": scope_value,
                "payload": payload_value,
            }
        )
    except (TypeError, ValueError) as exc:
        return _event_error(
            "OPTIMIZATION_EVENT_INVALID",
            "Optimization observer event is not canonical JSON",
            cause={"name": type(exc).__name__, "message": str(exc)},
        )
    if not isinstance(frozen, FrozenDict):
        return _event_error("OPTIMIZATION_EVENT_INVALID", "Optimization observer event must be an object")
    return ok(frozen)


def _find_forbidden_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_HOLDOUT_KEYS:
                return normalized
            nested = _find_forbidden_key(item)
            if nested is not None:
                return nested
    elif isinstance(value, list | tuple):
        for item in value:
            nested = _find_forbidden_key(item)
            if nested is not None:
                return nested
    return None


def _freeze_mapping(value: Mapping[str, Any], label: str) -> FrozenDict:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenDict):
        raise TypeError(f"{label} must be a mapping")
    return frozen


def _runtime_status(event_type: str) -> str:
    if event_type.endswith(".failed"):
        return "failed"
    if event_type.endswith(".completed") or event_type.endswith(".decided"):
        return "completed"
    if event_type.endswith(".started") or event_type.endswith(".requested"):
        return "running"
    return "event"


def _event_error(code: str, message: str, *, cause: Any = None, **metadata: Any) -> Result:
    return err(make_loom_error(code, message, retryable=False, cause=cause, metadata=metadata))


__all__ = [
    "OptimizationEventEmitter",
    "OptimizationSnapshot",
    "ScopedOptimizationTraceSink",
    "build_optimization_event",
]
