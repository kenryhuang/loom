"""Reusable Plan & Execute state for Loom loops."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

from loom.core import (
    Constraint,
    MinimalLoopDefinition,
    Observation,
    Result,
    StepResult,
    ToolRef,
    err,
    make_loom_error,
    now_iso,
    ok,
    thaw_json,
)
from loom.llm import LlmStepPolicy
from loom.runtime.workflow_routing import (
    WorkflowRouteController,
    WorkflowRoutePhase,
    WorkflowRoutePolicy,
    WorkflowRouteState,
    workflow_route_state_dict,
    workflow_route_state_from_mapping,
)

PLAN_TOOL_IDS = frozenset({"enter_plan", "continue_react", "submit_plan", "update_plan"})
_PLAN_TRANSITION_BOUNDARY = {
    "controlFlow": {
        "stepBoundary": True,
        "reason": "planning_transition",
    }
}
_WORKFLOW_ROUTE_TRANSITION_BOUNDARY = {
    "controlFlow": {
        "stepBoundary": True,
        "reason": "workflow_routing_transition",
    }
}


def _with_plan_transition_boundary(observation: Observation) -> Observation:
    metadata = dict(observation.metadata or {})
    metadata.update(_PLAN_TRANSITION_BOUNDARY)
    return replace(observation, metadata=metadata)


class PlanMode(StrEnum):
    AUTO = "auto"
    FORCE = "force"
    OFF = "off"


class PlanPhase(StrEnum):
    INACTIVE = "inactive"
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"


class PlanItemStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PlanItem:
    id: str
    content: str
    status: PlanItemStatus = PlanItemStatus.PENDING
    note: str | None = None


@dataclass(frozen=True, slots=True)
class PlanState:
    plan_id: str | None
    phase: PlanPhase
    reason: str | None
    explanation: str | None
    revision: int
    items: tuple[PlanItem, ...]
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class PlanEvent:
    event_type: str
    trigger: str
    explanation: str | None
    plan: PlanState


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex}"


class PlanController:
    def __init__(
        self,
        mode: PlanMode | str,
        *,
        id_factory: Callable[[str], str] = _new_id,
        now: Callable[[], str] = now_iso,
    ) -> None:
        self.mode = PlanMode(mode)
        self._id_factory = id_factory
        self._now = now
        self._events: list[PlanEvent] = []
        self._next_item_number = 1
        self._state = PlanState(None, PlanPhase.INACTIVE, None, None, 0, (), None, None)
        if self.mode is PlanMode.FORCE:
            self._enter("Plan mode was forced by the caller", trigger="cli_force")

    @property
    def state(self) -> PlanState:
        return self._state

    def enter(self, reason: str, *, trigger: str = "llm") -> Result:
        if self.mode is PlanMode.OFF:
            return self._error("PLAN_DISABLED", "Plan mode is disabled")
        if self._state.phase is not PlanPhase.INACTIVE:
            return self._error("PLAN_PHASE_INVALID", "A plan can only be entered from the inactive phase")
        if not reason.strip():
            return self._error("PLAN_REASON_REQUIRED", "Plan entry reason is required")
        self._enter(reason.strip(), trigger=trigger)
        return ok(self._state)

    def _enter(self, reason: str, *, trigger: str) -> None:
        at = self._now()
        self._state = PlanState(self._id_factory("plan_"), PlanPhase.PLANNING, reason, None, 0, (), at, at)
        self._events.append(PlanEvent("plan.entered", trigger, None, self._state))

    def submit(self, explanation: str, items: Sequence[str | Mapping[str, Any]]) -> Result:
        if self._state.phase is not PlanPhase.PLANNING:
            return self._error("PLAN_PHASE_INVALID", "A plan can only be submitted while planning")
        normalized: list[PlanItem] = []
        for item in items:
            content = item.get("content") if isinstance(item, Mapping) else item
            if not isinstance(content, str) or not content.strip():
                return self._error("PLAN_ITEM_INVALID", "Plan items must contain non-empty content")
            normalized.append(PlanItem(self._id_factory("step_"), content.strip()))
            self._next_item_number += 1
        if not normalized:
            return self._error("PLAN_ITEMS_REQUIRED", "A plan must contain at least one item")
        at = self._now()
        self._state = replace(
            self._state,
            phase=PlanPhase.EXECUTING,
            explanation=explanation.strip(),
            revision=self._state.revision + 1,
            items=tuple(normalized),
            updated_at=at,
        )
        self._events.append(PlanEvent("plan.submitted", "llm", self._state.explanation, self._state))
        return ok(self._state)

    def update(self, explanation: str, items: Sequence[Mapping[str, Any]]) -> Result:
        """Replace the editable plan snapshot while preserving terminal history."""
        if self._state.phase is not PlanPhase.EXECUTING:
            return self._error("PLAN_PHASE_INVALID", "A plan can only be updated while executing")
        if not items:
            return self._error("PLAN_ITEMS_REQUIRED", "A plan must contain at least one item")

        current_by_id = {item.id: item for item in self._state.items}
        normalized: list[tuple[str | None, str, PlanItemStatus, str | None]] = []
        seen_ids: set[str] = set()
        active_count = 0
        for raw in items:
            if not isinstance(raw, Mapping):
                return self._error("PLAN_ITEM_INVALID", "Plan items must be objects")
            item_id = raw.get("id")
            if item_id is not None and (not isinstance(item_id, str) or not item_id.strip()):
                return self._error("PLAN_ITEM_INVALID", "Plan item IDs must be non-empty strings")
            if item_id is not None:
                item_id = item_id.strip()
                if item_id in seen_ids:
                    return self._error("PLAN_DUPLICATE_ITEM_ID", f"Duplicate plan item ID: {item_id}")
                if item_id not in current_by_id:
                    return self._error("PLAN_UNKNOWN_ITEM", f"Unknown plan item ID: {item_id}")
                seen_ids.add(item_id)

            content = raw.get("content")
            if not isinstance(content, str) or not content.strip():
                return self._error("PLAN_ITEM_INVALID", "Plan items must contain non-empty content")
            try:
                status = PlanItemStatus(raw.get("status", PlanItemStatus.PENDING.value))
            except (TypeError, ValueError):
                return self._error("PLAN_ITEM_INVALID", "Plan item status is invalid")
            note = raw.get("note")
            if note is not None and not isinstance(note, str):
                return self._error("PLAN_ITEM_INVALID", "Plan item notes must be strings or null")
            note = note.strip() if isinstance(note, str) else None
            if status is PlanItemStatus.SKIPPED and not note:
                return self._error("PLAN_SKIP_REASON_REQUIRED", "Skipped plan items require a reason")
            if status is PlanItemStatus.IN_PROGRESS:
                active_count += 1
            normalized.append((item_id, content.strip(), status, note))

        if active_count > 1:
            return self._error("PLAN_MULTIPLE_ACTIVE_ITEMS", "At most one plan item may be in progress")

        terminal = {PlanItemStatus.COMPLETED, PlanItemStatus.SKIPPED}
        for index, old_item in enumerate(self._state.items):
            if old_item.status not in terminal:
                continue
            if index >= len(normalized):
                return self._error("PLAN_TERMINAL_HISTORY_IMMUTABLE", "Completed and skipped plan items cannot be removed")
            new_id, content, status, note = normalized[index]
            if (new_id, content, status, note) != (
                old_item.id,
                old_item.content,
                old_item.status,
                old_item.note,
            ):
                return self._error(
                    "PLAN_TERMINAL_HISTORY_IMMUTABLE",
                    "Completed and skipped plan items cannot be changed or reordered",
                )

        updated_items = tuple(PlanItem(item_id or self._id_factory("step_"), content, status, note) for item_id, content, status, note in normalized)
        at = self._now()
        self._state = replace(
            self._state,
            explanation=explanation.strip(),
            revision=self._state.revision + 1,
            items=updated_items,
            updated_at=at,
        )
        self._events.append(PlanEvent("plan.updated", "llm", self._state.explanation, self._state))
        return ok(self._state)

    def complete(self, *, trigger: str = "llm") -> Result:
        if self._state.phase is not PlanPhase.EXECUTING:
            return self._error("PLAN_PHASE_INVALID", "A plan can only be completed while executing")
        terminal = {PlanItemStatus.COMPLETED, PlanItemStatus.SKIPPED}
        if not self._state.items or any(item.status not in terminal for item in self._state.items):
            return self._error("PLAN_INCOMPLETE", "All plan items must be completed or skipped before finishing")
        self._state = replace(
            self._state,
            phase=PlanPhase.COMPLETED,
            revision=self._state.revision + 1,
            updated_at=self._now(),
        )
        self._events.append(PlanEvent("plan.completed", trigger, self._state.explanation, self._state))
        return ok(self._state)

    def drain_events(self) -> tuple[PlanEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    def restore(self, state: PlanState) -> None:
        """Restore trusted state persisted by the loop wrapper."""
        self._state = state
        self._events.clear()

    @staticmethod
    def _error(code: str, message: str) -> Result:
        return err(make_loom_error(code, message, retryable=False))


class PlanningRuntime:
    """Expose planning transitions as tools and guard execution tools."""

    def __init__(
        self,
        mode: PlanMode | str,
        *,
        finish_tool_id: str = "finish",
        id_factory: Callable[[str], str] = _new_id,
        now: Callable[[], str] = now_iso,
        route_policy: WorkflowRoutePolicy | None = None,
    ) -> None:
        self.mode = PlanMode(mode)
        self.finish_tool_id = finish_tool_id
        self._now = now
        self._controller = PlanController(self.mode, id_factory=id_factory, now=now)
        self._route = WorkflowRouteController(self.mode, policy=route_policy, now=now)
        self._route_invalid_attempts = 0
        self._bound_runtime: Any | None = None
        self._bound_context: Any | None = None
        self._normal_refs: tuple[ToolRef, ...] | None = None
        self._progress: list[dict[str, Any]] = []

    @property
    def controller(self) -> PlanController:
        return self._controller

    @property
    def route(self) -> WorkflowRouteController:
        return self._route

    def snapshot(self) -> dict[str, Any]:
        from loom.runtime.checkpoints import encode

        return {
            "schema_version": 1,
            "mode": self.mode.value,
            "plan": plan_state_dict(self._controller.state),
            "route": workflow_route_state_dict(self._route.state),
            "next_item_number": self._controller._next_item_number,
            "invalid_attempts": self._route_invalid_attempts,
            "normal_refs": encode(self._normal_refs),
            "progress": self._progress,
        }

    def restore(self, value: Mapping[str, Any]) -> Result:
        from loom.runtime.checkpoints import decode

        try:
            if value["schema_version"] != 1 or value["mode"] != self.mode.value:
                raise ValueError("Incompatible planning checkpoint")
            plan = plan_state_from_mapping(value["plan"])
            route = workflow_route_state_from_mapping(value["route"])
            refs = decode(value["normal_refs"])
            next_number = int(value["next_item_number"])
            invalid_attempts = int(value["invalid_attempts"])
            if next_number < 1 or invalid_attempts < 0:
                raise ValueError("Invalid planning counters")
            self._controller.restore(plan)
            self._controller._next_item_number = next_number
            self._route.restore(route)
            self._route_invalid_attempts = invalid_attempts
            self._normal_refs = refs
            self._progress = list(value.get("progress", []))[-12:]
            self._controller._events.clear()
            self._route._events.clear()
            return ok(None)
        except (KeyError, TypeError, ValueError) as exc:
            return err(make_loom_error("VALIDATION_FAILED", str(exc), retryable=False))

    def tool_refs(self) -> tuple[ToolRef, ...]:
        if self.mode is PlanMode.OFF:
            return ()
        item_schema = {
            "type": "object",
            "properties": {"content": {"type": "string", "minLength": 1}},
            "required": ["content"],
            "additionalProperties": False,
        }
        update_item_schema = {
            "type": "object",
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "content": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "enum": [status.value for status in PlanItemStatus],
                },
                "note": {"type": ["string", "null"]},
            },
            "required": ["content", "status"],
            "additionalProperties": False,
        }
        return (
            ToolRef(
                "enter_plan",
                "Enter planning mode when the task needs a multi-step workflow.",
                input_schema={
                    "type": "object",
                    "properties": {"reason": {"type": "string", "minLength": 1}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            ),
            ToolRef(
                "continue_react",
                "Continue direct execution. During progress review, name the missing evidence and the next action needed to obtain it.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "minLength": 1},
                        "evidence_gap": {"type": "string", "minLength": 1},
                        "next_action": {"type": "string", "minLength": 1},
                    },
                    "required": ["reason", "evidence_gap", "next_action"] if self._route.state.phase is WorkflowRoutePhase.REVIEWING else ["reason"],
                    "additionalProperties": False,
                },
            ),
            ToolRef(
                "submit_plan",
                "Submit the initial ordered checklist after entering planning mode.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "explanation": {"type": "string"},
                        "items": {"type": "array", "minItems": 1, "items": item_schema},
                    },
                    "required": ["explanation", "items"],
                    "additionalProperties": False,
                },
            ),
            ToolRef(
                "update_plan",
                "Submit the complete checklist snapshot to update progress or revise future "
                "non-terminal steps. Include every item that must remain; omitted non-terminal "
                "items are removed.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "explanation": {"type": "string"},
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "description": ("The complete replacement checklist. Include every item that must remain; omitted non-terminal items are removed."),
                            "items": update_item_schema,
                        },
                    },
                    "required": ["explanation", "items"],
                    "additionalProperties": False,
                },
            ),
        )

    def wrap_tools(self, handlers: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.mode is PlanMode.OFF:
            return handlers

        wrapped = {tool_id: self._guard_handler(tool_id, handler) for tool_id, handler in handlers.items()}

        async def enter_plan(input_value: Mapping[str, Any], _options: Any = None) -> Result:
            reason = str(input_value.get("reason", ""))
            if self._controller.state.phase is not PlanPhase.INACTIVE:
                return self._rejected("PLAN_PHASE_INVALID", "A plan can only be entered from the inactive phase")
            route = self._route.select_plan(reason)
            if not route.ok:
                return self._routing_attempt_rejected(route.error.code, route.error.message)
            self._route_invalid_attempts = 0
            return await self._transition_result(
                "enter_plan",
                self._controller.enter(reason),
            )

        async def continue_react(input_value: Mapping[str, Any], _options: Any = None) -> Result:
            if self._route.state.phase is WorkflowRoutePhase.REVIEWING:
                if any(not isinstance(input_value.get(key), str) or not input_value[key].strip() for key in ("reason", "evidence_gap", "next_action")):
                    return self._route_rejected(
                        "PROGRESS_EVIDENCE_REQUIRED",
                        "If the goal is fulfilled, call finish. Otherwise provide evidence_gap and next_action explaining why more work is necessary.",
                    )
                reason = f"{input_value.get('reason', '')}; missing evidence: {input_value['evidence_gap']}; next action: {input_value['next_action']}"
            else:
                reason = str(input_value.get("reason", ""))
            route = self._route.select_react(reason)
            if not route.ok:
                return self._routing_attempt_rejected(route.error.code, route.error.message)
            self._route_invalid_attempts = 0
            emitted = await self._emit_pending_events()
            if not emitted.ok:
                return emitted
            return ok(
                Observation(
                    _new_id("obs_"),
                    "continue_react",
                    {"accepted": True, "route": workflow_route_state_dict(route.value)},
                    self._now(),
                    metadata=_WORKFLOW_ROUTE_TRANSITION_BOUNDARY,
                )
            )

        async def submit_plan(input_value: Mapping[str, Any], _options: Any = None) -> Result:
            return await self._transition_result(
                "submit_plan",
                self._controller.submit(
                    str(input_value.get("explanation", "")),
                    input_value.get("items", ()),
                ),
            )

        async def update_plan(input_value: Mapping[str, Any], _options: Any = None) -> Result:
            return await self._transition_result(
                "update_plan",
                self._controller.update(
                    str(input_value.get("explanation", "")),
                    input_value.get("items", ()),
                ),
            )

        wrapped.update(
            {
                "enter_plan": enter_plan,
                "continue_react": continue_react,
                "submit_plan": submit_plan,
                "update_plan": update_plan,
            }
        )
        return wrapped

    def visible_tool_refs(self, normal_refs: Sequence[ToolRef]) -> tuple[ToolRef, ...]:
        """Return the tools the model may use in the controller's current phase."""
        if self.mode is PlanMode.OFF:
            return tuple(normal_refs)
        refs = {tool.id: tool for tool in self.tool_refs()}
        state = self._controller.state
        route = self._route.state
        if self.mode is PlanMode.AUTO and state.phase is PlanPhase.INACTIVE:
            if route.phase is WorkflowRoutePhase.UNDECIDED:
                return (refs["enter_plan"], refs["continue_react"])
            if route.phase is WorkflowRoutePhase.REVIEWING:
                finish = tuple(tool for tool in normal_refs if tool.id == self.finish_tool_id)
                return (refs["enter_plan"], refs["continue_react"], *finish)
            if route.phase is WorkflowRoutePhase.COMPLETED:
                return ()
            if route.phase is WorkflowRoutePhase.REACT:
                return (*normal_refs, refs["enter_plan"])
        if state.phase is PlanPhase.INACTIVE:
            return (*normal_refs, refs["enter_plan"])
        if state.phase is PlanPhase.PLANNING:
            return (refs["submit_plan"],)
        if state.phase is PlanPhase.EXECUTING:
            return (*normal_refs, refs["update_plan"])
        return tuple(normal_refs)

    def configure_normal_tool_refs(self, normal_refs: Sequence[ToolRef]) -> None:
        """Retain normal tools when an initial forced-planning context hides them."""
        self._normal_refs = tuple(normal_refs)

    def step_policy(self, _context: Any) -> LlmStepPolicy:
        route = self._route.state
        if self.mode is PlanMode.AUTO and route.phase in {
            WorkflowRoutePhase.UNDECIDED,
            WorkflowRoutePhase.REVIEWING,
        }:
            return LlmStepPolicy(
                tool_choice="auto",
                preserve_all_tools=True,
                require_tool_call=True,
                missing_tool_call_retries=1,
                invalid_tool_call_retries=1,
                retry_prompt=(
                    "Review current-request progress: call finish if fulfilled; otherwise continue_react with reason, "
                    "evidence_gap and next_action, or enter_plan for a revised workflow. "
                    "Task tools are unavailable until a route is selected. Make an actual tool call, not just a description or top-level fields. "
                    'If returning JSON, use {"reasoning":"Why more evidence is necessary","action":{"kind":"tool",'
                    '"target":"continue_react","input":{"reason":"Why direct execution is sufficient",'
                    '"evidence_gap":"Specific missing evidence","next_action":"Specific action to obtain it"}}}. '
                    "Replace example values with the current request's evidence."
                    if route.phase is WorkflowRoutePhase.REVIEWING
                    else "Call exactly one of enter_plan or continue_react with a concise reason. "
                    'If returning JSON, put the tool ID in action.target and its arguments in action.input.'
                ),
                failure_code="WORKFLOW_ROUTE_FAILED",
            )
        return LlmStepPolicy()

    def observe_tool(self, _context: Any, observation: Observation) -> Observation:
        if (
            self.mode is not PlanMode.AUTO
            or self._route.state.phase is not WorkflowRoutePhase.REACT
            or observation.source in PLAN_TOOL_IDS
            or observation.source == self.finish_tool_id
        ):
            return observation
        value = thaw_json(observation.value)
        failed = isinstance(value, Mapping) and (value.get("ok") is False or (value.get("exit_code", 0) != 0 and value.get("status") != "no_match"))
        stable = {k: v for k, v in value.items() if k not in {"duration_ms"}} if isinstance(value, Mapping) else value
        digest = hashlib.sha256(json.dumps([observation.source, stable], sort_keys=True, default=str).encode()).hexdigest()
        preview = json.dumps(value, ensure_ascii=False, default=str)
        preview = preview if len(preview) <= 1200 else preview[:800] + " ... " + preview[-400:]
        self._progress.append({"tool": observation.source, "fingerprint": digest, "outcome": "failed" if failed else "succeeded", "evidence": preview})
        self._progress = self._progress[-12:]
        review = self._route.observe_tool(failed=failed)
        if not review and sum(item["fingerprint"] == digest for item in self._progress) >= 3:
            review = self._route.request_review(
                "repeated_work", "The same operation produced the same evidence at least three times; check whether the current goal is already fulfilled"
            ).unwrap()
        if not review:
            return observation
        metadata = dict(observation.metadata or {})
        metadata["controlFlow"] = {
            "stepBoundary": True,
            "reason": "workflow_route_review",
        }
        return replace(observation, metadata=metadata)

    def request_route_review(self, trigger: str, reason: str) -> Result:
        """Accept an optional external signal such as future context compaction."""
        return self._route.request_review(trigger, reason)

    def _routing_attempt_rejected(self, code: str, message: str) -> Result:
        self._route_invalid_attempts += 1
        if self._route_invalid_attempts >= 2:
            return err(
                make_loom_error(
                    "WORKFLOW_ROUTE_FAILED",
                    "The model failed to select a valid workflow route after one retry",
                    retryable=False,
                    cause={"code": code, "message": message},
                )
            )
        return self._route_rejected(code, message)

    def wrap_loop(self, definition: MinimalLoopDefinition) -> MinimalLoopDefinition:
        if self.mode is PlanMode.OFF:
            return definition

        async def planned_step(context: Any, runtime: Any) -> Result:
            self._sync_from_context(context)
            if self._normal_refs is None:
                self._normal_refs = tuple(tool for tool in context.affordances.tools if tool.id not in PLAN_TOOL_IDS)
            self._bound_runtime = runtime
            self._bound_context = context
            try:
                emitted = await self._emit_pending_events()
                if not emitted.ok:
                    return emitted
                if self.mode is PlanMode.AUTO and self._route.state.phase is WorkflowRoutePhase.REACT:
                    self._route.mark_task_execution_started()
                prompt_context = self._project_context(context)
                result = definition.step(prompt_context, runtime)
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, Result):
                    result = ok(result)
                if not result.ok:
                    return result
                emitted = await self._emit_pending_events()
                if not emitted.ok:
                    return emitted
                return ok(self._with_plan_snapshot(result.value))
            finally:
                self._bound_runtime = None
                self._bound_context = None

        async def planned_done(context: Any, runtime: Any) -> Result:
            state = self._state_from_context(context) or self._controller.state
            route = self._route_state_from_context(context) or self._route.state
            if self.mode is PlanMode.AUTO and (
                route.phase in {WorkflowRoutePhase.UNDECIDED, WorkflowRoutePhase.REVIEWING}
                or (route.phase is WorkflowRoutePhase.REACT and not route.task_execution_started)
            ):
                return ok(False)
            if state.phase in {PlanPhase.PLANNING, PlanPhase.EXECUTING}:
                return ok(False)
            result = definition.done(context, runtime)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, Result) else ok(bool(result))

        return replace(definition, step=planned_step, done=planned_done)

    def _sync_from_context(self, context: Any) -> None:
        state = self._state_from_context(context)
        if state is not None and state != self._controller.state:
            self._controller.restore(state)
        route = self._route_state_from_context(context)
        if route is not None and route != self._route.state:
            self._route.restore(route)
        elif route is None and self.mode is PlanMode.AUTO and context.state.observations:
            inferred_phase = WorkflowRoutePhase.PLAN if state and state.phase is not PlanPhase.INACTIVE else WorkflowRoutePhase.REACT
            self._route.restore(
                WorkflowRouteState(
                    inferred_phase,
                    reason="Restored from task history",
                    trigger="context_restore",
                    task_execution_started=inferred_phase is WorkflowRoutePhase.REACT,
                )
            )

    @staticmethod
    def _state_from_context(context: Any) -> PlanState | None:
        scratch = context.state.scratch
        if not isinstance(scratch, Mapping):
            return None
        value = scratch.get("plan")
        return plan_state_from_mapping(value) if isinstance(value, Mapping) else None

    @staticmethod
    def _route_state_from_context(context: Any) -> WorkflowRouteState | None:
        scratch = context.state.scratch
        if not isinstance(scratch, Mapping):
            return None
        value = scratch.get("workflowRoute")
        return workflow_route_state_from_mapping(value) if isinstance(value, Mapping) else None

    def project_context(self, context: Any) -> Any:
        """Refresh current tools and progress instructions, including after resume."""
        return self._project_context(context)

    def _project_context(self, context: Any) -> Any:
        state = self._controller.state
        normal_refs = self._normal_refs or ()
        visible = self.visible_tool_refs(normal_refs)

        description = self._workflow_description(state)
        if self._route.state.phase is WorkflowRoutePhase.REVIEWING:
            description += "\nCurrent request: " + context.goal.objective
            description += "\nRecent operations and evidence (oldest first):\n" + "\n".join(
                f"- {item['tool']} [{item['outcome']}]: {item['evidence']}" for item in self._progress
            )
        workflow = Constraint("runtime-plan-workflow", description)
        constraints = tuple(constraint for constraint in context.identity.constraints if constraint.id != workflow.id)
        return replace(
            context,
            identity=replace(context.identity, constraints=(*constraints, workflow)),
            affordances=replace(context.affordances, tools=visible),
        )

    def _workflow_description(self, state: PlanState) -> str:
        if state.phase is PlanPhase.INACTIVE:
            route = self._route.state
            if route.phase is WorkflowRoutePhase.COMPLETED:
                return "The current request is fulfilled; return the committed final report without further tool calls."
            if self.mode is PlanMode.AUTO and route.phase in {
                WorkflowRoutePhase.UNDECIDED,
                WorkflowRoutePhase.REVIEWING,
            }:
                if route.phase is WorkflowRoutePhase.REVIEWING:
                    return (
                        f"Review progress for the current request, triggered by {route.trigger}: {route.reason}. "
                        "Compare existing evidence against the actual requested outcome. Choose exactly one: "
                        "finish with the final report if the goal is fulfilled; continue_react only with a concrete "
                        "evidence_gap, next_action and reason why they are necessary; enter_plan to revise the workflow "
                        "if dependencies or failures require replanning. Do not repeat successful operations or expand "
                        "into unrelated audits. Missing capabilities or unverified source claims do not count as success. "
                        "Make an actual tool call: in JSON, action.kind must be tool, action.target must name the selected "
                        "available tool, and action.input must contain its arguments. Put evidence_gap and next_action inside "
                        "continue_react's action.input, not at the top level. Task tools become available in the next step "
                        "after continue_react is accepted."
                    )
                review = (
                    "initial task routing" if route.phase is WorkflowRoutePhase.UNDECIDED else f"runtime review triggered by {route.trigger}: {route.reason}"
                )
                return (
                    f"Choose the workflow for {review}. Call exactly one routing tool. "
                    "Use enter_plan for three or more dependent phases, investigation followed by implementation "
                    "and verification, multiple modules or artifacts, high uncertainty, or work needing explicit "
                    "progress tracking. Use continue_react for a one-shot query, read-only lookup, single local edit, "
                    "or a few independent actions. Give a concise reason; do not execute task tools in this step."
                )
            return (
                "Execute only what the current request needs. Reuse evidence from successful operations, avoid repeating them, "
                "and finish once the requested outcome is supported. Use enter_plan if dependent steps require a revised workflow."
            )
        if state.phase is PlanPhase.PLANNING:
            return "Planning is active. Submit an ordered checklist with submit_plan before executing task tools."
        checklist = "\n".join(f"- [{_status_mark(item.status)}] {item.id}: {item.content}" + (f" — {item.note}" if item.note else "") for item in state.items)
        if state.phase is PlanPhase.EXECUTING:
            prefix = (
                "Execute the active checklist item using as many normal tools as needed. Call update_plan with the "
                "complete checklist snapshot only when item status, a material note, or future work changes. When "
                "moving forward, mark the current item completed or skipped and the next item in_progress in the "
                "same update. Keep exactly one item in_progress during execution. Finish only after every item is terminal."
            )
        else:
            prefix = "The checklist is complete; provide the final response."
        return f"{prefix}\nCurrent plan (revision {state.revision}):\n{checklist}"

    def _with_plan_snapshot(self, value: Any) -> StepResult:
        if not isinstance(value, StepResult):
            raise TypeError("Planning loop step must return StepResult")
        context = value.context
        scratch = dict(context.state.scratch or {})
        scratch["plan"] = plan_state_dict(self._controller.state)
        scratch["workflowRoute"] = workflow_route_state_dict(self._route.state)
        planned_context = replace(context, state=replace(context.state, scratch=scratch))
        return replace(value, context=planned_context)

    def _guard_handler(self, tool_id: str, handler: Any) -> Callable[..., Any]:
        async def guarded(input_value: Any, options: Any = None) -> Result:
            phase = self._controller.state.phase
            if self._route.state.phase is WorkflowRoutePhase.COMPLETED:
                return self._route_rejected("WORKFLOW_ROUTE_PHASE_INVALID", "The current request is complete; no further task tools may be used")
            if (
                self.mode is PlanMode.AUTO
                and self._route.state.phase
                in {
                    WorkflowRoutePhase.UNDECIDED,
                    WorkflowRoutePhase.REVIEWING,
                }
                and not (self._route.state.phase is WorkflowRoutePhase.REVIEWING and tool_id == self.finish_tool_id)
            ):
                return self._route_rejected(
                    "WORKFLOW_ROUTE_PHASE_INVALID",
                    "Choose enter_plan or continue_react before using task tools",
                )
            if phase is PlanPhase.COMPLETED:
                return self._rejected(
                    "PLAN_PHASE_INVALID",
                    "The plan is complete; no further task tools may be used",
                )
            if phase is PlanPhase.PLANNING:
                return self._rejected(
                    "PLAN_PHASE_INVALID",
                    "Submit the plan before using execution tools",
                )
            if phase is PlanPhase.EXECUTING:
                if tool_id == self.finish_tool_id:
                    terminal = {PlanItemStatus.COMPLETED, PlanItemStatus.SKIPPED}
                    if not self._controller.state.items or any(item.status not in terminal for item in self._controller.state.items):
                        return self._rejected(
                            "PLAN_INCOMPLETE",
                            "All plan items must be completed or skipped before finishing",
                        )
                elif sum(item.status is PlanItemStatus.IN_PROGRESS for item in self._controller.state.items) != 1:
                    return self._rejected(
                        "PLAN_ACTIVE_ITEM_REQUIRED",
                        "Exactly one plan item must be in progress before using execution tools",
                    )

            result = await _invoke_handler(handler, input_value, options)
            rejected = (
                result.ok
                and isinstance(result.value, Observation)
                and isinstance(result.value.value, Mapping)
                and (result.value.value.get("accepted") is False or result.value.value.get("completed") is False)
            )
            if result.ok and not rejected and phase is PlanPhase.EXECUTING and tool_id == self.finish_tool_id:
                completed = self._controller.complete(trigger="finish_tool")
                if not completed.ok:
                    return self._rejected(completed.error.code, completed.error.message)
                emitted = await self._emit_pending_events()
                if not emitted.ok:
                    return emitted
                return ok(_with_plan_transition_boundary(result.value))
            if (
                result.ok
                and not rejected
                and tool_id == self.finish_tool_id
                and isinstance(result.value, Observation)
                and isinstance(result.value.value, Mapping)
                and result.value.value.get("completed")
            ):
                self._route.complete()
                emitted = await self._emit_pending_events()
                if not emitted.ok:
                    return emitted
                return ok(_with_plan_transition_boundary(result.value))
            return result

        return guarded

    async def _transition_result(self, tool_id: str, transition: Result) -> Result:
        if not transition.ok:
            return self._rejected(transition.error.code, transition.error.message)
        emitted = await self._emit_pending_events()
        if not emitted.ok:
            return emitted
        return ok(
            Observation(
                _new_id("obs_"),
                tool_id,
                {
                    "accepted": True,
                    "plan": plan_state_dict(transition.value),
                    "route": workflow_route_state_dict(self._route.state),
                },
                self._now(),
                metadata=_PLAN_TRANSITION_BOUNDARY,
            )
        )

    def _rejected(self, code: str, message: str) -> Result:
        return ok(
            Observation(
                _new_id("obs_"),
                "planning.guard",
                {
                    "accepted": False,
                    "code": code,
                    "message": message,
                    "plan": plan_state_dict(self._controller.state),
                },
                self._now(),
            )
        )

    def _route_rejected(self, code: str, message: str) -> Result:
        return ok(
            Observation(
                _new_id("obs_"),
                "workflow.guard",
                {
                    "accepted": False,
                    "code": code,
                    "message": message,
                    "route": workflow_route_state_dict(self._route.state),
                },
                self._now(),
            )
        )

    async def _emit_pending_events(self) -> Result:
        if self._bound_runtime is None or self._bound_context is None:
            return ok(None)
        for event in self._route.drain_events():
            emitted = await self._bound_runtime.trace_sink.emit(
                {
                    "type": event.event_type,
                    "run_id": self._bound_context.run_id,
                    "loop_id": self._bound_runtime.loop_id,
                    "trace_id": self._bound_runtime.trace_id,
                    "step_number": len(self._bound_context.state.observations),
                    "revision": event.state.revision,
                    "trigger": event.trigger,
                    "reason": event.reason,
                    "route": event.route.value if event.route is not None else None,
                    "review_count": event.state.review_count,
                    "workflow_route": workflow_route_state_dict(event.state),
                    "at": event.at,
                }
            )
            if not emitted.ok:
                return emitted
        for event in self._controller.drain_events():
            emitted = await self._bound_runtime.trace_sink.emit(
                {
                    "type": event.event_type,
                    "run_id": self._bound_context.run_id,
                    "loop_id": self._bound_runtime.loop_id,
                    "trace_id": self._bound_runtime.trace_id,
                    "step_number": len(self._bound_context.state.observations),
                    "plan_id": event.plan.plan_id,
                    "revision": event.plan.revision,
                    "trigger": event.trigger,
                    "explanation": event.explanation,
                    "plan": plan_state_dict(event.plan),
                    "at": self._now(),
                }
            )
            if not emitted.ok:
                return emitted
        return ok(None)


async def _invoke_handler(handler: Any, input_value: Any, options: Any) -> Result:
    invoke = getattr(handler, "invoke", handler)
    result = invoke(input_value, options)
    if hasattr(result, "__await__"):
        result = await result
    return result if isinstance(result, Result) else ok(result)


def _status_mark(status: PlanItemStatus) -> str:
    return {
        PlanItemStatus.PENDING: " ",
        PlanItemStatus.IN_PROGRESS: "~",
        PlanItemStatus.COMPLETED: "x",
        PlanItemStatus.SKIPPED: "-",
    }[status]


def plan_state_dict(state: PlanState) -> dict[str, Any]:
    """Return a JSON-safe snapshot suitable for context scratch and events."""
    return {
        "plan_id": state.plan_id,
        "phase": state.phase.value,
        "reason": state.reason,
        "explanation": state.explanation,
        "revision": state.revision,
        "items": [
            {
                "id": item.id,
                "content": item.content,
                "status": item.status.value,
                "note": item.note,
            }
            for item in state.items
        ],
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


def plan_state_from_mapping(value: Mapping[str, Any]) -> PlanState:
    """Restore a plan snapshot previously produced by :func:`plan_state_dict`."""
    return PlanState(
        plan_id=value.get("plan_id"),
        phase=PlanPhase(value["phase"]),
        reason=value.get("reason"),
        explanation=value.get("explanation"),
        revision=int(value.get("revision", 0)),
        items=tuple(
            PlanItem(
                id=str(item["id"]),
                content=str(item["content"]),
                status=PlanItemStatus(item.get("status", PlanItemStatus.PENDING.value)),
                note=item.get("note"),
            )
            for item in value.get("items", ())
        ),
        created_at=value.get("created_at"),
        updated_at=value.get("updated_at"),
    )


__all__ = [
    "PlanController",
    "PlanEvent",
    "PlanItem",
    "PlanItemStatus",
    "PlanMode",
    "PlanPhase",
    "PlanState",
    "PLAN_TOOL_IDS",
    "PlanningRuntime",
    "plan_state_dict",
    "plan_state_from_mapping",
]
