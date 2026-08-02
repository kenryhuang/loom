"""Workflow routing state for automatic ReAct versus Plan selection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from loom.core import Result, err, make_loom_error, now_iso, ok


class WorkflowRoutePhase(StrEnum):
    UNDECIDED = "undecided"
    REACT = "react"
    REVIEWING = "reviewing"
    PLAN = "plan"


@dataclass(frozen=True, slots=True)
class WorkflowRoutePolicy:
    failure_threshold: int = 2
    tool_call_threshold: int = 12
    cooldown_calls: int = 6
    max_reviews: int = 3

    def __post_init__(self) -> None:
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if self.tool_call_threshold <= 0:
            raise ValueError("tool_call_threshold must be positive")
        if self.cooldown_calls < 0:
            raise ValueError("cooldown_calls must be non-negative")
        if self.max_reviews < 0:
            raise ValueError("max_reviews must be non-negative")


@dataclass(frozen=True, slots=True)
class WorkflowRouteState:
    phase: WorkflowRoutePhase
    revision: int = 0
    reason: str | None = None
    trigger: str | None = None
    review_count: int = 0
    calls_since_review: int = 0
    consecutive_failures: int = 0
    cooldown_remaining: int = 0
    task_execution_started: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowRouteEvent:
    event_type: str
    trigger: str
    reason: str
    route: WorkflowRoutePhase | None
    state: WorkflowRouteState
    at: str


class WorkflowRouteController:
    def __init__(
        self,
        mode: str,
        *,
        policy: WorkflowRoutePolicy | None = None,
        now: Callable[[], str] = now_iso,
    ) -> None:
        self.mode = str(getattr(mode, "value", mode))
        self.policy = policy or WorkflowRoutePolicy()
        self._now = now
        self._events: list[WorkflowRouteEvent] = []
        if self.mode == "auto":
            self._state = WorkflowRouteState(
                WorkflowRoutePhase.UNDECIDED,
                trigger="initial",
            )
            self._emit_requested("initial", "Determine whether this task needs an explicit plan")
        elif self.mode == "force":
            self._state = WorkflowRouteState(
                WorkflowRoutePhase.PLAN,
                reason="Plan mode was forced by the caller",
                trigger="cli_force",
            )
        elif self.mode == "off":
            self._state = WorkflowRouteState(
                WorkflowRoutePhase.REACT,
                reason="Plan mode is disabled",
                trigger="cli_off",
                task_execution_started=True,
            )
        else:
            raise ValueError(f"Unknown workflow route mode: {mode}")

    @property
    def state(self) -> WorkflowRouteState:
        return self._state

    def select_react(self, reason: str) -> Result:
        clean_reason = reason.strip()
        if not clean_reason:
            return self._error("WORKFLOW_ROUTE_REASON_REQUIRED", "Workflow route reason is required")
        if self._state.phase not in {WorkflowRoutePhase.UNDECIDED, WorkflowRoutePhase.REVIEWING}:
            return self._error("WORKFLOW_ROUTE_PHASE_INVALID", "ReAct can only be selected while routing")
        trigger = self._state.trigger or "initial"
        cooldown = self.policy.cooldown_calls if self._state.phase is WorkflowRoutePhase.REVIEWING else 0
        self._state = replace(
            self._state,
            phase=WorkflowRoutePhase.REACT,
            revision=self._state.revision + 1,
            reason=clean_reason,
            calls_since_review=0,
            consecutive_failures=0,
            cooldown_remaining=cooldown,
            task_execution_started=False,
        )
        self._emit_selected(trigger, clean_reason, WorkflowRoutePhase.REACT)
        return ok(self._state)

    def select_plan(self, reason: str) -> Result:
        clean_reason = reason.strip()
        if not clean_reason:
            return self._error("WORKFLOW_ROUTE_REASON_REQUIRED", "Workflow route reason is required")
        if self._state.phase not in {
            WorkflowRoutePhase.UNDECIDED,
            WorkflowRoutePhase.REACT,
            WorkflowRoutePhase.REVIEWING,
        }:
            return self._error("WORKFLOW_ROUTE_PHASE_INVALID", "Plan cannot be selected from the current route phase")
        trigger = "model" if self._state.phase is WorkflowRoutePhase.REACT else self._state.trigger or "initial"
        self._state = replace(
            self._state,
            phase=WorkflowRoutePhase.PLAN,
            revision=self._state.revision + 1,
            reason=clean_reason,
            trigger=trigger,
            calls_since_review=0,
            consecutive_failures=0,
            cooldown_remaining=0,
            task_execution_started=False,
        )
        self._emit_selected(trigger, clean_reason, WorkflowRoutePhase.PLAN)
        return ok(self._state)

    def mark_task_execution_started(self) -> WorkflowRouteState:
        if self._state.phase is WorkflowRoutePhase.REACT and not self._state.task_execution_started:
            self._state = replace(self._state, task_execution_started=True)
        return self._state

    def observe_tool(self, *, failed: bool) -> bool:
        if self.mode != "auto" or self._state.phase is not WorkflowRoutePhase.REACT or not self._state.task_execution_started:
            return False
        cooldown = max(0, self._state.cooldown_remaining - 1)
        calls = self._state.calls_since_review + 1
        failures = self._state.consecutive_failures + 1 if failed else 0
        self._state = replace(
            self._state,
            calls_since_review=calls,
            consecutive_failures=failures,
            cooldown_remaining=cooldown,
        )
        if cooldown > 0 or self._state.review_count >= self.policy.max_reviews:
            return False
        if failures >= self.policy.failure_threshold:
            return self._enter_review("tool_failures", f"{failures} consecutive tool failures")
        if calls >= self.policy.tool_call_threshold:
            return self._enter_review("tool_budget", f"{calls} task tool calls since the last workflow decision")
        return False

    def request_review(self, trigger: str, reason: str) -> Result:
        clean_trigger = trigger.strip()
        clean_reason = reason.strip()
        if not clean_trigger or not clean_reason:
            return self._error("WORKFLOW_ROUTE_REVIEW_INVALID", "Workflow review trigger and reason are required")
        eligible = (
            self.mode == "auto"
            and self._state.phase is WorkflowRoutePhase.REACT
            and self._state.task_execution_started
            and self._state.cooldown_remaining == 0
            and self._state.review_count < self.policy.max_reviews
        )
        return ok(self._enter_review(clean_trigger, clean_reason) if eligible else False)

    def drain_events(self) -> tuple[WorkflowRouteEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    def restore(self, state: WorkflowRouteState) -> None:
        self._state = state
        self._events.clear()

    def _enter_review(self, trigger: str, reason: str) -> bool:
        self._state = replace(
            self._state,
            phase=WorkflowRoutePhase.REVIEWING,
            revision=self._state.revision + 1,
            reason=reason,
            trigger=trigger,
            review_count=self._state.review_count + 1,
        )
        self._emit_requested(trigger, reason)
        return True

    def _emit_requested(self, trigger: str, reason: str) -> None:
        self._events.append(
            WorkflowRouteEvent(
                "workflow.routing.requested",
                trigger,
                reason,
                None,
                self._state,
                self._now(),
            )
        )

    def _emit_selected(self, trigger: str, reason: str, route: WorkflowRoutePhase) -> None:
        self._events.append(
            WorkflowRouteEvent(
                "workflow.route.selected",
                trigger,
                reason,
                route,
                self._state,
                self._now(),
            )
        )

    @staticmethod
    def _error(code: str, message: str) -> Result:
        return err(make_loom_error(code, message, retryable=False))


def workflow_route_state_dict(state: WorkflowRouteState) -> dict[str, Any]:
    return {
        "phase": state.phase.value,
        "revision": state.revision,
        "reason": state.reason,
        "trigger": state.trigger,
        "review_count": state.review_count,
        "calls_since_review": state.calls_since_review,
        "consecutive_failures": state.consecutive_failures,
        "cooldown_remaining": state.cooldown_remaining,
        "task_execution_started": state.task_execution_started,
    }


def workflow_route_state_from_mapping(value: Mapping[str, Any]) -> WorkflowRouteState:
    return WorkflowRouteState(
        phase=WorkflowRoutePhase(value["phase"]),
        revision=int(value.get("revision", 0)),
        reason=value.get("reason"),
        trigger=value.get("trigger"),
        review_count=int(value.get("review_count", 0)),
        calls_since_review=int(value.get("calls_since_review", 0)),
        consecutive_failures=int(value.get("consecutive_failures", 0)),
        cooldown_remaining=int(value.get("cooldown_remaining", 0)),
        task_execution_started=bool(value.get("task_execution_started", False)),
    )


__all__ = [
    "WorkflowRouteController",
    "WorkflowRouteEvent",
    "WorkflowRoutePhase",
    "WorkflowRoutePolicy",
    "WorkflowRouteState",
    "workflow_route_state_dict",
    "workflow_route_state_from_mapping",
]
