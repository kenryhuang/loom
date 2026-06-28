"""Async event collector that feeds trace events to the TUI."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loom.core.models import Result, ok


@dataclass
class TuiEvent:
    """Normalized event for TUI consumption."""

    timestamp: float
    event_type: str
    data: dict[str, Any]
    step_number: int | None = None
    trace_id: str | None = None
    llm_call_id: str | None = None
    tool_call_id: str | None = None
    run_id: str | None = None
    loop_id: str | None = None
    duration_ms: int | None = None
    outcome: str | None = None
    error: str | None = None


def _flatten(value: Any) -> Any:
    """Convert frozen dataclasses to plain dicts/lists for JSON display."""
    from dataclasses import fields, is_dataclass

    from loom.core.models import FrozenDict

    if isinstance(value, FrozenDict):
        return {k: _flatten(v) for k, v in value.items()}
    if isinstance(value, Mapping):
        return {str(k): _flatten(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_flatten(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _flatten(getattr(value, f.name)) for f in fields(value)}
    return value


class TuiEventCollector:
    """Trace sink that collects events and makes them available to the TUI via an async queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[TuiEvent] = asyncio.Queue()
        self._events: list[TuiEvent] = []
        self._llm_request_times: dict[str, float] = {}
        self._tool_request_times: dict[str, float] = {}
        self._step_request_times: dict[str, float] = {}
        self._done = False

    async def emit(self, event: Mapping[str, Any]) -> Result:
        """Receive a trace event and push it to the TUI queue."""
        event_type = str(event.get("type", "unknown"))
        plain = _flatten(event)
        now = time.monotonic()
        step_number = plain.get("step_number")
        trace_id = plain.get("trace_id")
        llm_call_id = plain.get("llm_call_id")
        tool_call_id = plain.get("tool_call_id")
        run_id = plain.get("run_id")
        loop_id = plain.get("loop_id")

        duration_ms = None
        outcome = None
        error = None

        # Track durations
        if event_type in {"llm.requested", "llm.stream.started"}:
            if llm_call_id:
                self._llm_request_times[llm_call_id] = now
        elif event_type in {"llm.stream.completed", "llm.completed"}:
            if llm_call_id and llm_call_id in self._llm_request_times:
                duration_ms = int((now - self._llm_request_times.pop(llm_call_id)) * 1000)
        elif event_type == "llm.failed":
            if llm_call_id and llm_call_id in self._llm_request_times:
                duration_ms = int((now - self._llm_request_times.pop(llm_call_id)) * 1000)
            err_data = plain.get("error")
            if isinstance(err_data, dict):
                error = err_data.get("message", str(err_data))
            elif err_data:
                error = str(err_data)
        elif event_type == "tool.started":
            if tool_call_id:
                self._tool_request_times[tool_call_id] = now
        elif event_type == "tool.completed":
            tid = tool_call_id or plain.get("tool_call_id")
            if tid and tid in self._tool_request_times:
                duration_ms = int((now - self._tool_request_times.pop(tid)) * 1000)
        elif event_type == "tool.failed":
            tid = tool_call_id or plain.get("tool_call_id")
            if tid and tid in self._tool_request_times:
                duration_ms = int((now - self._tool_request_times.pop(tid)) * 1000)
            err_data = plain.get("error")
            if isinstance(err_data, dict):
                error = err_data.get("message", str(err_data))
            elif err_data:
                error = str(err_data)
        elif event_type == "tool_selection.requested":
            sel_id = plain.get("selection_call_id")
            if sel_id:
                self._tool_request_times[sel_id] = now
        elif event_type == "tool_selection.decided":
            sel_id = plain.get("selection_call_id")
            if sel_id and sel_id in self._tool_request_times:
                duration_ms = int((now - self._tool_request_times.pop(sel_id)) * 1000)
        elif event_type == "tool_selection.failed":
            sel_id = plain.get("selection_call_id")
            if sel_id and sel_id in self._tool_request_times:
                duration_ms = int((now - self._tool_request_times.pop(sel_id)) * 1000)
            err_data = plain.get("error")
            if isinstance(err_data, dict):
                error = err_data.get("message", str(err_data))
            elif err_data:
                error = str(err_data)
        elif event_type == "step.started":
            if trace_id:
                self._step_request_times[trace_id] = now
        elif event_type == "step.completed":
            if trace_id and trace_id in self._step_request_times:
                duration_ms = int((now - self._step_request_times.pop(trace_id)) * 1000)
            trace_data = plain.get("trace", {})
            if isinstance(trace_data, dict):
                outcome = trace_data.get("outcome")
        elif event_type == "run.completed":
            outcome = plain.get("outcome")
            duration_ms = plain.get("duration_ms")

        tui_event = TuiEvent(
            timestamp=time.time(),
            event_type=event_type,
            data=plain,
            step_number=step_number,
            trace_id=trace_id,
            llm_call_id=llm_call_id,
            tool_call_id=tool_call_id,
            run_id=run_id,
            loop_id=loop_id,
            duration_ms=duration_ms,
            outcome=outcome,
            error=error,
        )

        self._events.append(tui_event)
        await self.queue.put(tui_event)
        return ok(None)

    def mark_done(self) -> None:
        """Signal that no more events will be emitted."""
        self._done = True

    async def put_sentinel(self) -> None:
        """Put a sentinel value to signal the TUI to stop waiting."""
        await self.queue.put(
            TuiEvent(
                timestamp=time.time(),
                event_type="_tui_done",
                data={},
            )
        )

    @property
    def events(self) -> list[TuiEvent]:
        return list(self._events)

    @property
    def event_count(self) -> int:
        return len(self._events)
