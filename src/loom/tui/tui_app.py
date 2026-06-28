"""Textual TUI app for Loom loop visualization — Codex/Claude style."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult, ScreenStackError
from textual.containers import Container, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Label, RichLog, Static

from loom.tui.tui_collector import TuiEvent

# ─── Color palette (Codex/Claude dark style) ───────────────────────────

COLORS = {
    "bg": "#1a1b26",
    "bg_dark": "#13141e",
    "bg_panel": "#1e1f2e",
    "border": "#3b3d57",
    "text": "#a9b1d6",
    "text_dim": "#565f89",
    "text_bright": "#c0caf5",
    "green": "#9ece6a",
    "cyan": "#7dcfff",
    "blue": "#7aa2f7",
    "magenta": "#bb9af7",
    "orange": "#ff9e64",
    "red": "#f7768e",
    "yellow": "#e0af68",
    "teal": "#73daca",
}

# ─── Event type styling ────────────────────────────────────────────────

EVENT_STYLES: dict[str, dict[str, str]] = {
    "run.started": {"icon": "▶", "color": COLORS["green"], "label": "RUN START"},
    "run.completed": {"icon": "✓", "color": COLORS["green"], "label": "RUN DONE"},
    "step.started": {"icon": "→", "color": COLORS["cyan"], "label": "STEP"},
    "step.completed": {"icon": "←", "color": COLORS["cyan"], "label": "STEP DONE"},
    "llm.requested": {"icon": "◎", "color": COLORS["magenta"], "label": "LLM →"},
    "llm.completed": {"icon": "◉", "color": COLORS["magenta"], "label": "LLM ←"},
    "llm.failed": {"icon": "✗", "color": COLORS["red"], "label": "LLM FAIL"},
    "llm.stream.started": {"icon": "◎", "color": COLORS["magenta"], "label": "LLM STREAM"},
    "llm.stream.completed": {"icon": "◉", "color": COLORS["magenta"], "label": "LLM DONE"},
    "llm.content.delta": {"icon": "…", "color": COLORS["magenta"], "label": "LLM TEXT"},
    "llm.reasoning.delta": {"icon": "…", "color": COLORS["blue"], "label": "LLM REASON"},
    "llm.reasoning_context.delta": {"icon": "…", "color": COLORS["blue"], "label": "LLM CTX"},
    "llm.tool_call.started": {"icon": "⚙", "color": COLORS["orange"], "label": "LLM TOOL"},
    "llm.tool_call.arguments.delta": {"icon": "⚙", "color": COLORS["orange"], "label": "TOOL ARGS"},
    "llm.tool_call.completed": {"icon": "⚙", "color": COLORS["green"], "label": "TOOL READY"},
    "tool.started": {"icon": "⚙", "color": COLORS["orange"], "label": "TOOL →"},
    "tool.completed": {"icon": "⚙", "color": COLORS["green"], "label": "TOOL ←"},
    "tool.failed": {"icon": "⚙", "color": COLORS["red"], "label": "TOOL FAIL"},
    "tool_selection.requested": {"icon": "🔧", "color": COLORS["teal"], "label": "TOOLSEL →"},
    "tool_selection.decided": {"icon": "🔧", "color": COLORS["teal"], "label": "TOOLSEL ✓"},
    "tool_selection.failed": {"icon": "🔧", "color": COLORS["red"], "label": "TOOLSEL ✗"},
    "_tui_done": {"icon": "■", "color": COLORS["text_dim"], "label": "END"},
}

LLM_PRESENTATION_EVENTS = {
    "llm.requested",
    "llm.completed",
    "llm.failed",
    "llm.stream.started",
    "llm.stream.completed",
    "llm.content.delta",
    "llm.reasoning.delta",
    "llm.reasoning_context.delta",
    "llm.tool_call.started",
    "llm.tool_call.arguments.delta",
    "llm.tool_call.completed",
}

TOOL_PRESENTATION_EVENTS = {
    "tool.started",
    "tool.completed",
    "tool.failed",
}


@dataclass
class _ToolCallStreamState:
    tool_call_id: str
    tool_name: str | None = None
    arguments_parts: list[str] = field(default_factory=list)
    completed: bool = False

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.tool_call_id,
            "name": self.tool_name,
            "arguments": "".join(self.arguments_parts),
            "completed": self.completed,
        }


@dataclass
class _LlmStreamState:
    first_event: TuiEvent
    metadata: dict[str, Any] = field(default_factory=dict)
    content_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    reasoning_context_parts: list[str] = field(default_factory=list)
    tool_calls: dict[str, _ToolCallStreamState] = field(default_factory=dict)
    response: Any | None = None
    error: Any | None = None
    delta_count: int = 0
    stream_started: bool = False
    completed: bool = False
    failed: bool = False
    duration_ms: int | None = None
    current_event: TuiEvent | None = None

    @classmethod
    def from_event(cls, event: TuiEvent) -> _LlmStreamState:
        state = cls(first_event=event)
        state.absorb(event)
        return state

    def absorb(self, event: TuiEvent) -> TuiEvent:
        self._merge_metadata(event)
        delta = event.data.get("delta")

        if event.event_type == "llm.stream.started":
            self.stream_started = True
        elif event.event_type == "llm.content.delta" and delta:
            self.content_parts.append(str(delta))
            self.delta_count += 1
        elif event.event_type == "llm.reasoning.delta" and delta:
            self.reasoning_parts.append(str(delta))
            self.delta_count += 1
        elif event.event_type == "llm.reasoning_context.delta" and delta:
            self.reasoning_context_parts.append(str(delta))
            self.delta_count += 1
        elif event.event_type == "llm.tool_call.started":
            tool = self._tool_state(event)
            tool.tool_name = _event_tool_name(event) or tool.tool_name
        elif event.event_type == "llm.tool_call.arguments.delta" and delta:
            tool = self._tool_state(event)
            tool.tool_name = _event_tool_name(event) or tool.tool_name
            tool.arguments_parts.append(str(delta))
            self.delta_count += 1
        elif event.event_type == "llm.tool_call.completed":
            tool = self._tool_state(event)
            tool.tool_name = _event_tool_name(event) or tool.tool_name
            tool.completed = True
        elif event.event_type == "llm.stream.completed":
            self.completed = True
            self.duration_ms = event.duration_ms
        elif event.event_type == "llm.completed":
            self.completed = True
            self.response = event.data.get("response")
            self.duration_ms = event.duration_ms or self.duration_ms
        elif event.event_type == "llm.failed":
            self.failed = True
            self.error = event.data.get("error") or event.error
            self.duration_ms = event.duration_ms or self.duration_ms

        self.current_event = self._to_event()
        return self.current_event

    def _merge_metadata(self, event: TuiEvent) -> None:
        for key, value in event.data.items():
            if key not in {"delta", "raw", "tool_call_id", "tool_name", "response", "error"}:
                self.metadata[key] = value

    def _tool_state(self, event: TuiEvent) -> _ToolCallStreamState:
        tool_call_id = _event_tool_call_id(event) or f"tool-{len(self.tool_calls) + 1}"
        tool = self.tool_calls.get(tool_call_id)
        if tool is None:
            tool = _ToolCallStreamState(tool_call_id=tool_call_id, tool_name=_event_tool_name(event))
            self.tool_calls[tool_call_id] = tool
        return tool

    def _to_event(self) -> TuiEvent:
        if self.failed:
            event_type = "llm.failed"
        elif self.response is not None:
            event_type = "llm.completed"
        elif self.completed:
            event_type = "llm.stream.completed"
        elif self.stream_started or self.delta_count or self.tool_calls:
            event_type = "llm.stream.started"
        else:
            event_type = "llm.requested"
        data = dict(self.metadata)
        status = "failed" if self.failed else "completed" if self.completed else "streaming" if self.stream_started else "requested"
        data.update(
            {
                "type": event_type,
                "status": status,
                "content": "".join(self.content_parts),
                "reasoning": "".join(self.reasoning_parts),
                "reasoning_context": "".join(self.reasoning_context_parts),
                "tool_calls": [tool.to_data() for tool in self.tool_calls.values()],
                "delta_count": self.delta_count,
            }
        )
        if self.response is not None:
            data["response"] = self.response
        if self.error is not None:
            data["error"] = self.error
        return replace(
            self.first_event,
            event_type=event_type,
            data=data,
            duration_ms=self.duration_ms,
        )


@dataclass
class _ToolExecutionState:
    first_event: TuiEvent
    metadata: dict[str, Any] = field(default_factory=dict)
    input_value: Any | None = None
    output_value: Any | None = None
    error: Any | None = None
    completed: bool = False
    failed: bool = False
    duration_ms: int | None = None
    current_event: TuiEvent | None = None

    @classmethod
    def from_event(cls, event: TuiEvent) -> _ToolExecutionState:
        state = cls(first_event=event)
        state.absorb(event)
        return state

    def absorb(self, event: TuiEvent) -> TuiEvent:
        self._merge_metadata(event)
        if "input" in event.data:
            self.input_value = event.data.get("input")
        if event.event_type == "tool.completed":
            self.completed = True
            self.output_value = event.data.get("output")
            self.duration_ms = event.duration_ms
        elif event.event_type == "tool.failed":
            self.failed = True
            self.error = event.data.get("error") or event.error
            self.duration_ms = event.duration_ms

        self.current_event = self._to_event()
        return self.current_event

    def _merge_metadata(self, event: TuiEvent) -> None:
        for key, value in event.data.items():
            if key not in {"input", "output", "error"}:
                self.metadata[key] = value

    def _to_event(self) -> TuiEvent:
        event_type = "tool.failed" if self.failed else "tool.completed" if self.completed else "tool.started"
        data = dict(self.metadata)
        data["type"] = event_type
        data["status"] = "failed" if self.failed else "completed" if self.completed else "running"
        if self.input_value is not None:
            data["input"] = self.input_value
        if self.output_value is not None:
            data["output"] = self.output_value
        if self.error is not None:
            data["error"] = self.error
        return replace(
            self.first_event,
            event_type=event_type,
            data=data,
            duration_ms=self.duration_ms,
            error=str(self.error) if self.error is not None else None,
        )


def _event_tool_call_id(event: TuiEvent) -> str | None:
    value = event.tool_call_id or event.data.get("tool_call_id")
    return str(value) if value else None


def _event_tool_name(event: TuiEvent) -> str | None:
    value = event.data.get("tool_name")
    return str(value) if value else None


def _event_llm_call_id(event: TuiEvent) -> str | None:
    value = event.llm_call_id or event.data.get("llm_call_id")
    return str(value) if value else None


def _event_tool_execution_key(event: TuiEvent) -> str:
    tool_call_id = _event_tool_call_id(event)
    if tool_call_id:
        return tool_call_id
    tool_id = str(event.data.get("tool_id") or "unknown")
    trace_id = event.trace_id or str(event.data.get("trace_id") or "")
    step_number = event.step_number if event.step_number is not None else event.data.get("step_number", "")
    started_at = event.data.get("started_at") or event.data.get("at") or ""
    return f"{trace_id}:{step_number}:{tool_id}:{started_at}"


def _format_event_line(event: TuiEvent) -> Text:
    """Format a single event for the timeline."""
    style = EVENT_STYLES.get(event.event_type, {"icon": "•", "color": COLORS["text_dim"], "label": event.event_type})
    icon = style["icon"]
    color = style["color"]
    label = style["label"]

    parts: list[tuple[str, str]] = []
    parts.append((f" {icon} ", color))
    parts.append((f"{label:<12}", color))

    if event.step_number is not None:
        parts.append((f"step:{event.step_number} ", COLORS["text_dim"]))

    # Add context-specific info
    if event.event_type == "llm.requested":
        model = event.data.get("model", "")
        parts.append((model, COLORS["blue"]))
    elif event.event_type == "llm.completed":
        if event.duration_ms is not None:
            parts.append((f"{event.duration_ms}ms ", COLORS["text_dim"]))
        resp = event.data.get("response", {})
        if isinstance(resp, dict):
            usage = resp.get("usage", {})
            if isinstance(usage, dict):
                total = usage.get("total_tokens", 0)
                if total:
                    parts.append((f"{total}tok ", COLORS["yellow"]))
    elif event.event_type.startswith("llm.") and event.event_type.endswith(".delta"):
        delta = str(event.data.get("delta", ""))
        parts.append((delta[:60], COLORS["text_dim"]))
    elif event.event_type in {"llm.tool_call.started", "llm.tool_call.completed"}:
        tool = event.data.get("tool_name") or event.data.get("tool_call_id") or ""
        parts.append((str(tool)[:60], COLORS["orange"]))
    elif event.event_type == "tool.started":
        tool_id = event.data.get("tool_id", "")
        parts.append((tool_id, COLORS["orange"]))
    elif event.event_type == "tool.completed":
        tool_id = event.data.get("tool_id", "")
        parts.append((tool_id, COLORS["green"]))
        if event.duration_ms is not None:
            parts.append((f" {event.duration_ms}ms", COLORS["text_dim"]))
    elif event.event_type == "run.completed":
        outcome = event.data.get("outcome", "")
        steps = event.data.get("steps", 0)
        parts.append((f"{outcome} ", COLORS["green"]))
        parts.append((f"{steps} steps", COLORS["text_dim"]))
    elif event.event_type in ("llm.failed", "tool.failed"):
        err = event.error or "unknown error"
        parts.append((err[:60], COLORS["red"]))
    elif event.event_type == "tool_selection.decided":
        selected = event.data.get("selected_tools", [])
        excluded = event.data.get("excluded_tools", [])
        if event.duration_ms is not None:
            parts.append((f"{event.duration_ms}ms ", COLORS["text_dim"]))
        usage = event.data.get("token_usage", {})
        if isinstance(usage, dict):
            total = usage.get("total_tokens", 0)
            if total:
                parts.append((f"{total}tok ", COLORS["yellow"]))
        parts.append((f"select:{len(selected)} ", COLORS["teal"]))
        parts.append((f"exclude:{len(excluded)}", COLORS["text_dim"]))
    elif event.event_type == "tool_selection.failed":
        err = event.error or "unknown error"
        parts.append((err[:60], COLORS["red"]))

    text = Text()
    for content, style_color in parts:
        text.append(content, style=style_color)
    return text


def _format_event_detail(event: TuiEvent) -> str:
    """Format full event detail for the detail panel."""
    lines: list[str] = []
    style = EVENT_STYLES.get(event.event_type, {"icon": "•", "color": COLORS["text_dim"], "label": event.event_type})

    lines.append(f"[bold {COLORS['cyan']}]{style['icon']} {style['label']}[/]")
    lines.append("")

    # Core metadata
    lines.append(f"[dim]type:[/]      {event.event_type}")
    if event.run_id:
        lines.append(f"[dim]run_id:[/]    {event.run_id}")
    if event.loop_id:
        lines.append(f"[dim]loop_id:[/]   {event.loop_id}")
    if event.trace_id:
        lines.append(f"[dim]trace_id:[/]  {event.trace_id}")
    if event.step_number is not None:
        lines.append(f"[dim]step:[/]      {event.step_number}")
    if event.duration_ms is not None:
        lines.append(f"[dim]duration:[/]  {event.duration_ms}ms")
    if event.outcome:
        lines.append(f"[dim]outcome:[/]   {event.outcome}")
    if event.error:
        lines.append(f"[{COLORS['red']}]error:[/]     {event.error}")
    lines.append("")

    # Event-specific detail
    data = event.data

    if event.event_type == "llm.requested":
        lines.append(f"[bold {COLORS['magenta']}]─── LLM Request ───[/]")
        _append_llm_input_details(lines, data)

    elif event.event_type == "llm.completed":
        if data.get("messages") or data.get("tools"):
            lines.append(f"[bold {COLORS['magenta']}]─── LLM Input ───[/]")
            _append_llm_input_details(lines, data)

        if _has_llm_stream_details(data):
            lines.append(f"[bold {COLORS['magenta']}]─── LLM SSE ───[/]")
            _append_llm_stream_details(lines, data)

        lines.append(f"[bold {COLORS['magenta']}]─── LLM Response ───[/]")
        resp = data.get("response", {})
        if isinstance(resp, dict):
            content = resp.get("content")
            if content:
                lines.append(f"[bold {COLORS['magenta']}]content:[/]")
                if not _append_llm_content(lines, content, indent="  "):
                    _append_wrapped(lines, str(content), indent="  ")
                lines.append("")
            tool_calls = resp.get("tool_calls", [])
            if isinstance(tool_calls, (list, tuple)) and tool_calls:
                lines.append(f"[bold {COLORS['orange']}]tool_calls:[/]")
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        lines.append(f"  [{COLORS['orange']}]{tc.get('name', '?')}[/]({tc.get('id', '')[:8]})")
                        args = tc.get("arguments", "{}")
                        if not _append_jsonish(lines, args, indent="    "):
                            lines.append(f"    {args}")
                lines.append("")
            usage = resp.get("usage", {})
            if isinstance(usage, dict):
                pt = usage.get("prompt_tokens", 0)
                ct = usage.get("completion_tokens", 0)
                tt = usage.get("total_tokens", 0)
                lines.append(f"[dim]tokens:[/] prompt={pt} completion={ct} total={tt}")
            finish = resp.get("finish_reason")
            if finish:
                lines.append(f"[dim]finish_reason:[/] {finish}")
        lines.append("")

    elif event.event_type in {"llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta"}:
        lines.append(f"[bold {COLORS['magenta']}]─── LLM Stream ───[/]")
        delta = data.get("delta", "")
        if not _append_jsonish(lines, delta, indent="  "):
            _append_wrapped(lines, str(delta), indent="  ")
        lines.append("")

    elif event.event_type == "llm.tool_call.arguments.delta":
        lines.append(f"[bold {COLORS['orange']}]─── Tool Arguments ───[/]")
        tool = data.get("tool_name") or data.get("tool_call_id") or "unknown"
        lines.append(f"[dim]tool:[/] {tool}")
        delta = data.get("delta", "")
        if not _append_jsonish(lines, delta, indent="  "):
            _append_wrapped(lines, str(delta), indent="  ")
        lines.append("")

    elif event.event_type in {"llm.stream.started", "llm.stream.completed", "llm.tool_call.started", "llm.tool_call.completed"}:
        lines.append(f"[bold {COLORS['magenta']}]─── LLM Stream ───[/]")
        model = data.get("model")
        if model:
            lines.append(f"[dim]model:[/] {model}")
        status = data.get("status")
        if status:
            lines.append(f"[dim]status:[/] {status}")
        tool = data.get("tool_name") or data.get("tool_call_id")
        if tool:
            lines.append(f"[dim]tool:[/] {tool}")
        if event.duration_ms is not None:
            lines.append(f"[dim]duration:[/] {event.duration_ms}ms")
        delta_count = data.get("delta_count")
        if delta_count:
            lines.append(f"[dim]chunks:[/] {delta_count}")
        lines.append("")

        _append_llm_stream_details(lines, data)
        lines.append("")

    elif event.event_type == "tool.started":
        lines.append(f"[bold {COLORS['orange']}]─── Tool Call ───[/]")
        lines.append(f"[dim]tool:[/] {data.get('tool_id', '?')}")
        inp = data.get("input")
        if inp is not None:
            lines.append("[dim]input:[/]")
            if not _append_jsonish(lines, inp, indent="  "):
                lines.append(f"  {inp}")
        lines.append("")

    elif event.event_type == "tool.completed":
        lines.append(f"[bold {COLORS['green']}]─── Tool Result ───[/]")
        lines.append(f"[dim]tool:[/] {data.get('tool_id', '?')}")
        inp = data.get("input")
        if inp is not None:
            lines.append("[dim]input:[/]")
            if not _append_jsonish(lines, inp, indent="  "):
                lines.append(f"  {inp}")
        out = data.get("output")
        if out is not None:
            lines.append("[dim]output:[/]")
            value = out.value if hasattr(out, "value") else out
            if not _append_jsonish(lines, value, indent="  "):
                lines.append(f"  {value}")
        lines.append("")

    elif event.event_type == "tool.failed":
        lines.append(f"[bold {COLORS['red']}]─── Tool Failed ───[/]")
        lines.append(f"[dim]tool:[/] {data.get('tool_id', '?')}")
        inp = data.get("input")
        if inp is not None:
            lines.append("[dim]input:[/]")
            if not _append_jsonish(lines, inp, indent="  "):
                lines.append(f"  {inp}")
        err_data = data.get("error") or event.error
        if err_data:
            lines.append(f"[{COLORS['red']}]error:[/] {err_data}")
        lines.append("")

    elif event.event_type == "step.completed":
        lines.append(f"[bold {COLORS['cyan']}]─── Step Complete ───[/]")
        trace = data.get("trace", {})
        if isinstance(trace, dict):
            lines.append(f"[dim]outcome:[/] {trace.get('outcome', '?')}")
            decisions = trace.get("decisions", [])
            if isinstance(decisions, (list, tuple)) and decisions:
                lines.append(f"[bold {COLORS['blue']}]decisions:[/]")
                for d in decisions:
                    if isinstance(d, dict):
                        action = d.get("action", {})
                        if isinstance(action, dict):
                            lines.append(f"  action: [{COLORS['blue']}]{action.get('kind', '?')}[/] - {action.get('description', '')}")
                        reasoning = d.get("reasoning", "")
                        if reasoning:
                            _append_wrapped(lines, f"reasoning: {reasoning}", indent="  ")
                        lines.append(f"  confidence: {d.get('confidence', 0)}")
        lines.append("")

    elif event.event_type == "run.completed":
        lines.append(f"[bold {COLORS['green']}]─── Run Complete ───[/]")
        lines.append(f"[dim]outcome:[/]   {data.get('outcome', '?')}")
        lines.append(f"[dim]steps:[/]     {data.get('steps', 0)}")
        lines.append(f"[dim]duration:[/]  {data.get('duration_ms', 0)}ms")
        lines.append(f"[dim]traces:[/]    {data.get('trace_count', 0)}")
        lines.append("")

    elif event.event_type == "run.started":
        lines.append(f"[bold {COLORS['green']}]─── Run Started ───[/]")
        ctx_id = data.get("context_id", "")
        if ctx_id:
            lines.append(f"[dim]context:[/] {ctx_id}")
        meta = data.get("metadata", {})
        if isinstance(meta, dict) and meta:
            lines.append(f"[dim]metadata:[/] {json.dumps(meta, ensure_ascii=False)}")
        lines.append("")

    elif event.event_type == "tool_selection.requested":
        lines.append(f"[bold {COLORS['teal']}]─── Tool Selection ───[/]")
        lines.append(f"[dim]model:[/] {data.get('model', 'unknown')}")
        available = data.get("available_tools", [])
        if isinstance(available, list):
            lines.append(f"[dim]available:[/] {', '.join(available)}")
        lines.append("")

    elif event.event_type == "tool_selection.decided":
        lines.append(f"[bold {COLORS['teal']}]─── Tool Selection Decided ───[/]")
        lines.append(f"[dim]model:[/] {data.get('model', 'unknown')}")
        if event.duration_ms is not None:
            lines.append(f"[dim]duration:[/] {event.duration_ms}ms")
        usage = data.get("token_usage", {})
        if isinstance(usage, dict):
            lines.append(
                f"[dim]tokens:[/] prompt={usage.get('prompt_tokens', 0)} completion={usage.get('completion_tokens', 0)} total={usage.get('total_tokens', 0)}"
            )
        lines.append("")
        reasoning = data.get("reasoning", "")
        if reasoning:
            lines.append(f"[bold {COLORS['blue']}]reasoning:[/]")
            _append_wrapped(lines, reasoning, indent="  ")
            lines.append("")
        selected = data.get("selected_tools", [])
        excluded = data.get("excluded_tools", [])
        if isinstance(selected, list):
            lines.append(f"[bold {COLORS['teal']}]selected:[/]")
            for tid in selected:
                lines.append(f"  [{COLORS['green']}]✓ {tid}[/]")
        if isinstance(excluded, list) and excluded:
            lines.append(f"[bold {COLORS['text_dim']}]excluded:[/]")
            for tid in excluded:
                lines.append(f"  [{COLORS['text_dim']}]✗ {tid}[/]")
        conf = data.get("confidence", 0)
        lines.append(f"[dim]confidence:[/] {conf}")
        lines.append("")

    elif event.event_type == "tool_selection.failed":
        lines.append(f"[bold {COLORS['red']}]─── Tool Selection Failed ───[/]")
        lines.append(f"[dim]model:[/] {data.get('model', 'unknown')}")
        if event.error:
            lines.append(f"[{COLORS['red']}]error:[/] {event.error}")
        if event.duration_ms is not None:
            lines.append(f"[dim]duration:[/] {event.duration_ms}ms")
        lines.append("")

    else:
        # Generic: show remaining data
        interesting = {k: v for k, v in data.items() if k not in ("type", "run_id", "loop_id", "trace_id", "step_number", "at")}
        if interesting:
            lines.append("[dim]data:[/]")
            if not _append_jsonish(lines, interesting, indent="  ", max_chars=2000):
                lines.append(f"  {interesting}")
        lines.append("")

    return "\n".join(lines)


def _append_llm_input_details(lines: list[str], data: dict[str, Any]) -> None:
    lines.append(f"[dim]model:[/] {data.get('model', 'unknown')}")
    lines.append("")
    messages = data.get("messages", [])
    if isinstance(messages, (list, tuple)):
        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get("role", "?")
                content = msg.get("content", "")
                role_color = COLORS["blue"] if role == "system" else COLORS["green"] if role == "user" else COLORS["magenta"]
                lines.append(f"[bold {role_color}]{role}:[/]")
                if isinstance(content, str):
                    _append_wrapped(lines, content, indent="  ")
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            _append_wrapped(lines, item.get("text", ""), indent="  ")
                lines.append("")
    tools = data.get("tools", [])
    if tools:
        lines.append(f"[bold {COLORS['orange']}]tools:[/]")
        for tool in tools:
            if isinstance(tool, dict):
                fn = tool.get("function", {})
                if isinstance(fn, dict):
                    lines.append(f"  [{COLORS['orange']}]{fn.get('name', '?')}[/] - {fn.get('description', '')}")
        lines.append("")


def _has_llm_stream_details(data: dict[str, Any]) -> bool:
    return any(data.get(key) for key in ("content", "reasoning", "reasoning_context", "tool_calls"))


def _append_llm_stream_details(lines: list[str], data: dict[str, Any]) -> None:
    reasoning = data.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        lines.append(f"[bold {COLORS['blue']}]thinking:[/]")
        _append_wrapped(lines, reasoning, indent="  ")
        lines.append("")

    reasoning_context = data.get("reasoning_context")
    if isinstance(reasoning_context, str) and reasoning_context:
        lines.append(f"[bold {COLORS['blue']}]reasoning_context:[/]")
        _append_wrapped(lines, reasoning_context, indent="  ")
        lines.append("")

    tool_calls = data.get("tool_calls", [])
    if isinstance(tool_calls, (list, tuple)) and tool_calls:
        lines.append(f"[bold {COLORS['orange']}]tool_calls:[/]")
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or "?"
            tid = str(tc.get("id") or "")
            status_text = "done" if tc.get("completed") else "streaming"
            lines.append(f"  [{COLORS['orange']}]{name}[/]({tid[:8]}) [{COLORS['text_dim']}]{status_text}[/]")
            arguments = tc.get("arguments")
            if isinstance(arguments, str) and arguments and not _append_jsonish(lines, arguments, indent="    "):
                _append_wrapped(lines, arguments, indent="    ")
        lines.append("")

    content = data.get("content")
    if isinstance(content, str) and content:
        lines.append(f"[bold {COLORS['magenta']}]content:[/]")
        _append_wrapped(lines, content, indent="  ")
        lines.append("")


def _append_jsonish(lines: list[str], value: Any, *, indent: str = "", max_chars: int | None = None) -> bool:
    """Append dict/list values or JSON strings as readable structured text."""
    parsed = _jsonish_value(value)
    if parsed is None:
        return False

    rendered_lines: list[str] = []
    _append_jsonish_rendered(rendered_lines, parsed, indent=indent)
    if max_chars is not None:
        rendered = "\n".join(rendered_lines)
        rendered = rendered[:max_chars]
        lines.extend(rendered.splitlines())
    else:
        lines.extend(rendered_lines)
    return True


def _append_llm_content(lines: list[str], value: Any, *, indent: str = "") -> bool:
    parsed = _jsonish_value(value)
    if parsed is None:
        return False
    report = _extract_report_text(parsed)
    if report is not None:
        _append_wrapped(lines, report)
        return True
    _append_jsonish_rendered(lines, parsed, indent=indent)
    return True


def _append_jsonish_rendered(lines: list[str], value: Any, *, indent: str = "", prefix: str = "", trailing_comma: bool = False) -> None:
    comma = "," if trailing_comma else ""

    if isinstance(value, dict):
        lines.append(f"{indent}{prefix}{{")
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            item_prefix = f"{json.dumps(str(key), ensure_ascii=False)}: "
            _append_jsonish_rendered(
                lines,
                item,
                indent=f"{indent}  ",
                prefix=item_prefix,
                trailing_comma=index < len(items) - 1,
            )
        lines.append(f"{indent}}}{comma}")
        return

    if isinstance(value, list | tuple):
        lines.append(f"{indent}{prefix}[")
        for index, item in enumerate(value):
            _append_jsonish_rendered(
                lines,
                item,
                indent=f"{indent}  ",
                trailing_comma=index < len(value) - 1,
            )
        lines.append(f"{indent}]{comma}")
        return

    if isinstance(value, str):
        normalized = _normalize_display_text(value)
        if "\n" in normalized:
            lines.append(f"{indent}{prefix}".rstrip())
            _append_wrapped(lines, normalized, indent=f"{indent}  ")
            return
        lines.append(f"{indent}{prefix}{json.dumps(normalized, ensure_ascii=False)}{comma}")
        return

    lines.append(f"{indent}{prefix}{json.dumps(value, ensure_ascii=False, default=str)}{comma}")


def _extract_report_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    action = value.get("action")
    if not isinstance(action, dict):
        return None
    input_value = action.get("input")
    if not isinstance(input_value, dict):
        return None
    report = input_value.get("report")
    return report if isinstance(report, str) else None


def _jsonish_value(value: Any) -> Any | None:
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            jsonish = _jsonish_value(item)
            normalized[key] = item if jsonish is None else jsonish
        return normalized
    if isinstance(value, list | tuple):
        normalized = []
        for item in value:
            jsonish = _jsonish_value(item)
            normalized.append(item if jsonish is None else jsonish)
        return normalized
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict | list) else None


def _append_wrapped(lines: list[str], text: str, indent: str = "", max_width: int = 80) -> None:
    """Append text with basic word wrapping."""
    if not text:
        return
    text = _normalize_display_text(text)
    prefix_len = len(indent)
    available = max_width - prefix_len
    for raw_line in text.splitlines():
        if not raw_line:
            lines.append("")
            continue
        words = raw_line.split()
        current = ""
        for word in words:
            if len(current) + len(word) + 1 <= available:
                current = f"{current} {word}" if current else word
            else:
                if current:
                    lines.append(f"{indent}{current}")
                current = word
        if current:
            lines.append(f"{indent}{current}")


def _normalize_display_text(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\t", "\t")


# ─── Widgets ───────────────────────────────────────────────────────────


class EventDetailBox(RichLog):
    """Fixed-height scrollable detail area embedded inside an event item."""

    DETAIL_HEIGHT = 12

    DEFAULT_CSS = f"""
    EventDetailBox {{
        height: {DETAIL_HEIGHT};
        max-height: {DETAIL_HEIGHT};
        background: {COLORS["bg_dark"]};
        border: tall {COLORS["border"]};
        padding: 0 1;
        margin: 0 0 1 2;
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=True, highlight=True, wrap=True, **kwargs)

    def set_event(self, event: TuiEvent) -> None:
        self.clear()
        self.write(_format_event_detail(event))
        self.scroll_end(animate=False)


class EventItem(Container):
    """One event row with an inline collapsible detail box."""

    DEFAULT_CSS = f"""
    EventItem {{
        width: 100%;
        height: auto;
        min-height: 1;
    }}
    EventItem.-selected {{
        background: {COLORS["bg_dark"]};
    }}
    .event-summary {{
        height: 1;
        color: {COLORS["text"]};
        padding: 0 1;
    }}
    .event-summary:hover {{
        background: {COLORS["border"]};
    }}
    """

    def __init__(self, event: TuiEvent, *, expanded: bool = False, selected: bool = False, pinned_expanded: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.event = event
        self.is_expanded = expanded
        self.is_selected = selected
        self.is_pinned_expanded = pinned_expanded
        self.detail_height = EventDetailBox.DETAIL_HEIGHT
        self._summary = Label(_format_event_line(self.event), classes="event-summary")
        self._detail = EventDetailBox(classes="event-detail")

    def compose(self) -> ComposeResult:
        yield self._summary
        yield self._detail

    def on_mount(self) -> None:
        self._apply_event()
        self._apply_state()

    def set_event(self, event: TuiEvent) -> None:
        self.event = event
        self._apply_event()

    def set_selected(self, selected: bool) -> None:
        self.is_selected = selected
        self._apply_state()

    def set_expanded(self, expanded: bool) -> None:
        self.is_expanded = expanded
        self._apply_state()

    def toggle_expanded(self) -> None:
        if self.is_pinned_expanded and self.is_expanded:
            self.is_pinned_expanded = False
        self.set_expanded(not self.is_expanded)

    def _apply_event(self) -> None:
        self._summary.update(_format_event_line(self.event))
        self._detail.set_event(self.event)

    def _apply_state(self) -> None:
        if self.is_selected:
            self.add_class("-selected")
        else:
            self.remove_class("-selected")
        self._detail.display = self.is_expanded


class EventFeedWidget(VerticalScroll):
    """Single-column event stream with inline details."""

    DEFAULT_CSS = f"""
    EventFeedWidget {{
        background: {COLORS["bg_panel"]};
        border: tall {COLORS["border"]};
        height: 1fr;
        padding: 0 1;
    }}
    EventFeedWidget:focus {{
        border: tall {COLORS["cyan"]};
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._event_items: list[EventItem] = []
        self._selected_index = -1

    def compose(self) -> ComposeResult:
        yield Label(
            f"[bold {COLORS['cyan']}]  EVENT STREAM[/]",
            classes="event-feed-header",
        )
        yield Static("", classes="event-feed-separator")

    def add_event(self, event: TuiEvent, *, pinned_expanded: bool = False) -> None:
        """Append an event item and expand it as the active event."""
        for item in self._event_items:
            item.set_selected(False)
            if not item.is_pinned_expanded:
                item.set_expanded(False)

        item = EventItem(event, expanded=True, selected=True, pinned_expanded=pinned_expanded)
        self.mount(item)
        self._event_items.append(item)
        self._selected_index = len(self._event_items) - 1
        self.scroll_end(animate=False)

    def update_event(self, index: int, event: TuiEvent) -> None:
        """Update one existing event item in place."""
        if 0 <= index < len(self._event_items):
            self._event_items[index].set_event(event)

    def get_event(self, index: int) -> TuiEvent | None:
        if 0 <= index < len(self._event_items):
            return self._event_items[index].event
        return None

    def get_item(self, index: int) -> EventItem:
        return self._event_items[index]

    @property
    def event_count(self) -> int:
        return len(self._event_items)

    def select_event(self, index: int) -> TuiEvent | None:
        if not 0 <= index < len(self._event_items):
            return None

        if 0 <= self._selected_index < len(self._event_items) and self._selected_index != index:
            previous = self._event_items[self._selected_index]
            previous.set_selected(False)
            if not previous.is_pinned_expanded:
                previous.set_expanded(False)

        self._selected_index = index
        item = self._event_items[index]
        item.set_selected(True)
        item.set_expanded(True)
        return item.event

    def toggle_selected_detail(self) -> None:
        if 0 <= self._selected_index < len(self._event_items):
            self._event_items[self._selected_index].toggle_expanded()

    def get_selected_index(self) -> int:
        return self._selected_index


class StatusBar(Static):
    """Bottom status bar with metrics."""

    DEFAULT_CSS = f"""
    StatusBar {{
        dock: bottom;
        height: 1;
        background: {COLORS["bg_dark"]};
        color: {COLORS["text_dim"]};
        padding: 0 1;
    }}
    """

    steps = reactive(0)
    tokens = reactive(0)
    duration = reactive(0)
    status = reactive("idle")

    def render(self) -> str:
        parts = [
            f" [{COLORS['cyan']}]steps: {self.steps}[/]",
            f" [{COLORS['yellow']}]tokens: {self.tokens}[/]",
        ]
        if self.duration > 0:
            parts.append(f" [{COLORS['text_dim']}]{self.duration}ms[/]")
        status_color = COLORS["green"] if self.status == "completed" else COLORS["orange"] if self.status == "running" else COLORS["text_dim"]
        parts.append(f" [{status_color}]{self.status}[/]")
        return " │".join(parts)


class LoopHeader(Static):
    """Top header showing loop info."""

    DEFAULT_CSS = f"""
    LoopHeader {{
        dock: top;
        height: 3;
        background: {COLORS["bg_dark"]};
        padding: 0 1;
        border-bottom: tall {COLORS["border"]};
    }}
    """

    loop_role = reactive("")
    loop_goal = reactive("")
    loop_id = reactive("")

    def render(self) -> str:
        if not self.loop_role:
            return f"[bold {COLORS['cyan']}] LOOM TUI[/]  [dim]— loop visualization[/]"
        lines = [
            f"[bold {COLORS['cyan']}] LOOM TUI[/]  [dim]— {self.loop_role}[/]",
            f"  [dim]goal:[/] {self.loop_goal}",
        ]
        return "\n".join(lines)


# ─── Main App ──────────────────────────────────────────────────────────


class LoomTuiApp(App[None]):
    """Main TUI application for visualizing Loom loop execution."""

    CSS = f"""
    * {{
        scrollbar-color: {COLORS["border"]};
        scrollbar-color-hover: {COLORS["text_dim"]};
        scrollbar-background: {COLORS["bg_dark"]};
    }}

    Screen {{
        background: {COLORS["bg"]};
    }}

    .event-feed-header {{
        color: {COLORS["cyan"]};
        text-style: bold;
        padding: 1 0 0 0;
    }}

    .event-feed-separator {{
        height: 1;
        background: {COLORS["border"]};
    }}
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("g", "scroll_top", "Top"),
        ("G", "scroll_bottom", "Bottom"),
        ("enter", "toggle_detail", "Toggle Detail"),
        ("space", "toggle_detail", "Toggle Detail"),
    ]

    def __init__(self, collector: Any) -> None:
        super().__init__()
        self.collector = collector
        self._total_tokens = 0
        self._step_count = 0
        self._start_time = time.monotonic()
        self._run_done = False
        self._loop_role = ""
        self._loop_goal = ""
        self._llm_streams: dict[str, _LlmStreamState] = {}
        self._llm_stream_indices: dict[str, int] = {}
        self._tool_executions: dict[str, _ToolExecutionState] = {}
        self._tool_execution_indices: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield LoopHeader(id="loop_header")
        yield EventFeedWidget(id="event_feed")
        yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Start listening for events."""
        self._apply_loop_info()
        self.set_interval(0.05, self._poll_events)

    def _poll_events(self) -> None:
        """Poll the collector queue and update UI."""
        try:
            while True:
                event = self.collector.queue.get_nowait()
                self._handle_event(event)
        except asyncio.QueueEmpty:
            pass

    def _handle_event(self, event: TuiEvent) -> None:
        """Process a single event and update the UI."""
        if event.event_type == "_tui_done":
            self._run_done = True
            status_bar = self.query_one("#status", StatusBar)
            status_bar.status = "completed"
            return

        if self._handle_llm_event(event):
            return
        if self._handle_tool_event(event):
            return

        feed = self.query_one("#event_feed", EventFeedWidget)
        feed.add_event(event)

        # Update metrics
        if event.event_type == "step.completed":
            self._step_count += 1
            status_bar = self.query_one("#status", StatusBar)
            status_bar.steps = self._step_count
            status_bar.status = "running"

        if event.event_type == "llm.completed":
            resp = event.data.get("response", {})
            if isinstance(resp, dict):
                usage = resp.get("usage", {})
                if isinstance(usage, dict):
                    self._total_tokens += usage.get("total_tokens", 0)
                    status_bar = self.query_one("#status", StatusBar)
                    status_bar.tokens = self._total_tokens

        if event.event_type == "run.started":
            status_bar = self.query_one("#status", StatusBar)
            status_bar.status = "running"
            # Update header with loop info
            header = self.query_one("#loop_header", LoopHeader)
            meta = event.data.get("metadata", {})
            if isinstance(meta, dict):
                header.loop_role = meta.get("role", "loop")
                header.loop_goal = meta.get("objective", "")

        if event.event_type == "run.completed":
            status_bar = self.query_one("#status", StatusBar)
            status_bar.status = "completed"
            status_bar.duration = event.duration_ms or 0

    def _handle_llm_event(self, event: TuiEvent) -> bool:
        if event.event_type not in LLM_PRESENTATION_EVENTS:
            return False

        llm_call_id = _event_llm_call_id(event)
        if not llm_call_id:
            return False

        feed = self.query_one("#event_feed", EventFeedWidget)
        stream = self._llm_streams.get(llm_call_id)

        if stream is None:
            stream = _LlmStreamState.from_event(event)
            self._llm_streams[llm_call_id] = stream
            feed.add_event(stream.current_event or event, pinned_expanded=True)
            index = feed.event_count - 1
            self._llm_stream_indices[llm_call_id] = index
        else:
            aggregate = stream.absorb(event)
            index = self._llm_stream_indices[llm_call_id]
            feed.update_event(index, aggregate)

        feed.select_event(self._llm_stream_indices[llm_call_id])
        return True

    def _handle_tool_event(self, event: TuiEvent) -> bool:
        if event.event_type not in TOOL_PRESENTATION_EVENTS:
            return False

        key = _event_tool_execution_key(event)
        feed = self.query_one("#event_feed", EventFeedWidget)
        execution = self._tool_executions.get(key)

        if execution is None:
            execution = _ToolExecutionState.from_event(event)
            self._tool_executions[key] = execution
            feed.add_event(execution.current_event or event, pinned_expanded=True)
            index = feed.event_count - 1
            self._tool_execution_indices[key] = index
        else:
            aggregate = execution.absorb(event)
            index = self._tool_execution_indices[key]
            feed.update_event(index, aggregate)

        feed.select_event(self._tool_execution_indices[key])
        return True

    def action_cursor_down(self) -> None:
        """Move selection down in timeline."""
        feed = self.query_one("#event_feed", EventFeedWidget)
        idx = feed.get_selected_index()
        if idx < feed.event_count - 1:
            feed.select_event(idx + 1)

    def action_cursor_up(self) -> None:
        """Move selection up in timeline."""
        feed = self.query_one("#event_feed", EventFeedWidget)
        idx = feed.get_selected_index()
        if idx > 0:
            feed.select_event(idx - 1)

    def action_scroll_top(self) -> None:
        """Scroll to top of timeline."""
        feed = self.query_one("#event_feed", EventFeedWidget)
        if feed.event_count > 0:
            feed.select_event(0)
        feed.scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        """Scroll to bottom of timeline."""
        feed = self.query_one("#event_feed", EventFeedWidget)
        if feed.event_count > 0:
            feed.select_event(feed.event_count - 1)
        feed.scroll_end(animate=False)

    def action_toggle_detail(self) -> None:
        """Toggle the selected event detail."""
        feed = self.query_one("#event_feed", EventFeedWidget)
        feed.toggle_selected_detail()

    def set_loop_info(self, role: str, goal: str) -> None:
        """Set loop metadata in the header."""
        self._loop_role = role
        self._loop_goal = goal
        self._apply_loop_info()

    def _apply_loop_info(self) -> None:
        try:
            header = self.query_one("#loop_header", LoopHeader)
        except ScreenStackError:
            return
        header.loop_role = self._loop_role
        header.loop_goal = self._loop_goal
