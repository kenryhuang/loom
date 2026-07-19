"""Bounded, non-authoritative presentation state for the Optimize TUI."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from loom.core import Result, ok, thaw_json

PIPELINE_STAGES = (
    "preflight_complete",
    "seed_analysis_complete",
    "campaign_initialized",
    "search_running",
    "search_sealed",
    "validation_complete",
    "holdout_complete",
    "governance_complete",
    "report_complete",
)


@dataclass(slots=True)
class CandidateRow:
    candidate_id: str
    status: str = "admitted"
    surface: str | None = None
    phase: str | None = None
    completed_pairs: int = 0
    total_pairs: int | None = None
    score: float | None = None
    regression: float | None = None
    frontier: bool = False
    finalist: bool = False
    rejection: str | None = None


@dataclass(slots=True)
class TrialRow:
    trial_id: str
    candidate_id: str | None = None
    phase: str | None = None
    task_id: str | None = None
    repetition: int | None = None
    side: str | None = None
    status: str = "running"
    runtime_event: str | None = None
    solver_tokens: int = 0
    wall_time_seconds: int = 0


@dataclass(slots=True)
class BudgetState:
    used: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OptimizeDashboardState:
    optimization_id: str | None = None
    campaign_id: str | None = None
    stage: str = "created"
    lifecycle: str = "running"
    sequence: int = 0
    pipeline: dict[str, str] = field(default_factory=lambda: {stage: "pending" for stage in PIPELINE_STAGES})
    candidates: dict[str, CandidateRow] = field(default_factory=dict)
    active_trial: TrialRow | None = None
    budget: BudgetState = field(default_factory=BudgetState)
    holdout_status: str = "sealed"
    governance_status: str = "pending"
    terminal_status: str | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)


class OptimizeTuiCollector:
    """Reduce observer envelopes and notify the UI without blocking producers."""

    def __init__(self, *, max_recent_events: int = 2000) -> None:
        if max_recent_events < 1:
            raise ValueError("max_recent_events must be positive")
        self.max_recent_events = max_recent_events
        self.state = OptimizeDashboardState()
        self.updates: asyncio.Queue[None] = asyncio.Queue(maxsize=1)

    async def emit(self, event) -> Result:
        value = thaw_json(event)
        if not isinstance(value, dict):
            return ok(None)
        sequence = value.get("sequence")
        if not isinstance(sequence, int) or sequence <= self.state.sequence:
            return ok(None)
        self._reduce(value)
        self._retain(value)
        self.state.sequence = sequence
        with contextlib.suppress(asyncio.QueueFull):
            self.updates.put_nowait(None)
        return ok(None)

    def _reduce(self, event: dict[str, Any]) -> None:
        state = self.state
        event_type = str(event.get("type", ""))
        status = str(event.get("status", "event"))
        stage = event.get("stage")
        scope = event.get("scope") if isinstance(event.get("scope"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

        state.optimization_id = str(event.get("optimization_id") or state.optimization_id or "") or None
        state.campaign_id = str(event.get("campaign_id") or state.campaign_id or "") or None
        if isinstance(stage, str) and stage:
            state.stage = stage

        if event_type == "optimization.snapshot.loaded":
            state.lifecycle = str(payload.get("lifecycle", status))
            for completed in payload.get("completed_stages", ()):
                if isinstance(completed, str):
                    state.pipeline[completed] = "completed"
        elif event_type.startswith("optimization.stage.") and isinstance(stage, str):
            state.pipeline[stage] = status
            if status in {"failed", "paused"}:
                state.lifecycle = status

        candidate_id = scope.get("candidate_id") or payload.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            candidate = state.candidates.setdefault(candidate_id, CandidateRow(candidate_id))
            phase = scope.get("phase")
            if isinstance(phase, str):
                candidate.phase = phase
            if event_type == "optimization.candidate.admitted":
                candidate.status = "admitted"
                surface = payload.get("surface") or payload.get("changed_surface")
                if isinstance(surface, str):
                    candidate.surface = surface
            elif event_type in {"optimization.candidate.rejected", "optimization.proposal.rejected"}:
                candidate.status = "rejected"
                candidate.rejection = str(payload.get("code") or payload.get("reason") or "rejected")
            elif event_type == "optimization.trial.completed":
                candidate.completed_pairs += 1
            elif event_type == "optimization.experiment.started":
                trial_count = payload.get("trial_count")
                if isinstance(trial_count, int):
                    candidate.total_pairs = trial_count

        if event_type == "optimization.frontier.updated":
            members = payload.get("candidate_ids", payload.get("frontier", ()))
            member_ids = {str(value) for value in members} if isinstance(members, list | tuple) else set()
            for key, candidate in state.candidates.items():
                candidate.frontier = key in member_ids

        if event_type == "optimization.trial.started":
            state.active_trial = TrialRow(
                trial_id=str(scope.get("trial_id", "unknown")),
                candidate_id=_optional_text(scope.get("candidate_id")),
                phase=_optional_text(scope.get("phase")),
                task_id=_optional_text(scope.get("task_id")),
                repetition=scope.get("repetition") if isinstance(scope.get("repetition"), int) else None,
                side=_optional_text(scope.get("side")),
                status=status,
            )
        elif event_type in {"optimization.trial.completed", "optimization.trial.failed"}:
            if state.active_trial is not None and state.active_trial.trial_id == scope.get("trial_id"):
                state.active_trial.status = status
                state.active_trial.solver_tokens = _integer(payload.get("solver_tokens"))
                state.active_trial.wall_time_seconds = _integer(payload.get("wall_time_seconds"))
        elif event_type == "optimization.runtime.event" and state.active_trial is not None:
            runtime = payload.get("runtime_event")
            if isinstance(runtime, dict):
                state.active_trial.runtime_event = _optional_text(runtime.get("type"))

        if event_type == "optimization.budget.updated":
            if isinstance(payload.get("used"), dict):
                state.budget.used = dict(payload["used"])
            if isinstance(payload.get("limits"), dict):
                state.budget.limits = dict(payload["limits"])

        if event_type == "optimization.holdout.status":
            state.holdout_status = "sealed" if payload.get("sealed", True) else status
        if event_type.startswith("optimization.governance."):
            state.governance_status = status
            if status == "awaiting_approval":
                state.lifecycle = status
        if event_type in {"optimization.completed", "optimization.paused", "optimization.failed"}:
            state.terminal_status = status
            state.lifecycle = status

    def _retain(self, event: dict[str, Any]) -> None:
        key = _coalesce_key(event)
        if key is not None and self.state.recent_events and _coalesce_key(self.state.recent_events[-1]) == key:
            self.state.recent_events[-1] = event
            return
        self.state.recent_events.append(event)
        overflow = len(self.state.recent_events) - self.max_recent_events
        if overflow > 0:
            del self.state.recent_events[:overflow]


def _coalesce_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if event.get("type") != "optimization.runtime.event":
        return None
    payload = event.get("payload")
    runtime = payload.get("runtime_event") if isinstance(payload, dict) else None
    if not isinstance(runtime, dict) or runtime.get("type") not in {"llm.delta", "llm.stream.delta"}:
        return None
    call_id = runtime.get("llm_call_id") or runtime.get("call_id")
    scope = event.get("scope")
    trial_id = scope.get("trial_id") if isinstance(scope, dict) else None
    if not call_id:
        return None
    return ("llm.delta", str(trial_id or ""), str(call_id))


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _integer(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


__all__ = [
    "BudgetState",
    "CandidateRow",
    "OptimizeDashboardState",
    "OptimizeTuiCollector",
    "PIPELINE_STAGES",
    "TrialRow",
]
