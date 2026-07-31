"""Reusable Plan & Execute state for Loom loops."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

from loom.core import Result, err, make_loom_error, now_iso, ok


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
                return self._error(
                    "PLAN_TERMINAL_HISTORY_IMMUTABLE", "Completed and skipped plan items cannot be removed"
                )
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

        updated_items = tuple(
            PlanItem(item_id or self._id_factory("step_"), content, status, note)
            for item_id, content, status, note in normalized
        )
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

    @staticmethod
    def _error(code: str, message: str) -> Result:
        return err(make_loom_error(code, message, retryable=False))


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
    "plan_state_dict",
    "plan_state_from_mapping",
]
