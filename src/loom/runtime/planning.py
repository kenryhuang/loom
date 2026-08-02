"""Reusable Plan & Execute state for Loom loops."""

from __future__ import annotations

import inspect
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
)

PLAN_TOOL_IDS = frozenset({"enter_plan", "submit_plan", "update_plan"})
_PLAN_TRANSITION_BOUNDARY = {
    "controlFlow": {
        "stepBoundary": True,
        "reason": "planning_transition",
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
    ) -> None:
        self.mode = PlanMode(mode)
        self.finish_tool_id = finish_tool_id
        self._now = now
        self._controller = PlanController(self.mode, id_factory=id_factory, now=now)
        self._bound_runtime: Any | None = None
        self._bound_context: Any | None = None
        self._normal_refs: tuple[ToolRef, ...] | None = None

    @property
    def controller(self) -> PlanController:
        return self._controller

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
            return await self._transition_result(
                "enter_plan",
                self._controller.enter(str(input_value.get("reason", ""))),
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

    @staticmethod
    def _state_from_context(context: Any) -> PlanState | None:
        scratch = context.state.scratch
        if not isinstance(scratch, Mapping):
            return None
        value = scratch.get("plan")
        return plan_state_from_mapping(value) if isinstance(value, Mapping) else None

    def _project_context(self, context: Any) -> Any:
        state = self._controller.state
        normal_refs = self._normal_refs or ()
        visible = self.visible_tool_refs(normal_refs)

        workflow = Constraint("runtime-plan-workflow", self._workflow_description(state))
        constraints = tuple(constraint for constraint in context.identity.constraints if constraint.id != workflow.id)
        return replace(
            context,
            identity=replace(context.identity, constraints=(*constraints, workflow)),
            affordances=replace(context.affordances, tools=visible),
        )

    @staticmethod
    def _workflow_description(state: PlanState) -> str:
        if state.phase is PlanPhase.INACTIVE:
            return "Use enter_plan when the task requires multiple dependent steps; otherwise continue with the normal ReAct workflow."
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
        planned_context = replace(context, state=replace(context.state, scratch=scratch))
        return replace(value, context=planned_context)

    def _guard_handler(self, tool_id: str, handler: Any) -> Callable[..., Any]:
        async def guarded(input_value: Any, options: Any = None) -> Result:
            phase = self._controller.state.phase
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
            if result.ok and phase is PlanPhase.EXECUTING and tool_id == self.finish_tool_id:
                completed = self._controller.complete(trigger="finish_tool")
                if not completed.ok:
                    return self._rejected(completed.error.code, completed.error.message)
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
                {"accepted": True, "plan": plan_state_dict(transition.value)},
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

    async def _emit_pending_events(self) -> Result:
        if self._bound_runtime is None or self._bound_context is None:
            return ok(None)
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
