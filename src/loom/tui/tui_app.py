"""Textual TUI app for Loom loop visualization — Codex/Claude style."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
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

# ─── Event presentation grouping ───────────────────────────────────────

_RICH_TAG_RE = re.compile(r"\[(?:/|dim|bold(?: [^\]]+)?|#[0-9a-fA-F]{6})\]")

LLM_PRESENTATION_EVENTS = {
    "llm.requested",
    "llm.completed",
    "llm.failed",
    "llm.stream.started",
    "llm.stream.completed",
    "llm.content.delta",
    "llm.reasoning.delta",
    "llm.reasoning_context.delta",
}

TOOL_PRESENTATION_EVENTS = {
    "llm.tool_call.started",
    "llm.tool_call.arguments.delta",
    "llm.tool_call.completed",
    "tool.started",
    "tool.completed",
    "tool.failed",
}


@dataclass
class _LlmStreamState:
    first_event: TuiEvent
    metadata: dict[str, Any] = field(default_factory=dict)
    content_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    reasoning_context_parts: list[str] = field(default_factory=list)
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

    def _to_event(self) -> TuiEvent:
        if self.failed:
            event_type = "llm.failed"
        elif self.response is not None:
            event_type = "llm.completed"
        elif self.completed:
            event_type = "llm.stream.completed"
        elif self.stream_started or self.delta_count:
            event_type = "llm.stream.started"
        else:
            event_type = "llm.requested"
        data = dict(self.metadata)
        status = "failed" if self.failed else "completed" if self.completed else "streaming" if self.stream_started else "requested"
        elapsed_ms = self.duration_ms
        if elapsed_ms is None:
            elapsed_ms = max(0, int((time.time() - self.first_event.timestamp) * 1000)) if self.first_event.timestamp else None
        data.update(
            {
                "type": event_type,
                "status": status,
                "content": "".join(self.content_parts),
                "reasoning": "".join(self.reasoning_parts),
                "reasoning_context": "".join(self.reasoning_context_parts),
                "delta_count": self.delta_count,
                "token_count": self.delta_count,
            }
        )
        if elapsed_ms is not None:
            data["elapsed_ms"] = elapsed_ms
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
    tool_name: str | None = None
    arguments_parts: list[str] = field(default_factory=list)
    input_value: Any | None = None
    output_value: Any | None = None
    error: Any | None = None
    arguments_completed: bool = False
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
        self.tool_name = _event_tool_name(event) or self.tool_name
        if "input" in event.data:
            self.input_value = event.data.get("input")
        if event.event_type == "llm.tool_call.arguments.delta":
            delta = event.data.get("delta")
            if delta:
                self.arguments_parts.append(str(delta))
        elif event.event_type == "llm.tool_call.completed":
            self.arguments_completed = True
        elif event.event_type == "tool.completed":
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
            if key not in {"input", "output", "error", "delta"}:
                self.metadata[key] = value

    def _to_event(self) -> TuiEvent:
        event_type = "tool.failed" if self.failed else "tool.completed" if self.completed else "tool.started"
        data = dict(self.metadata)
        data["type"] = event_type
        data["status"] = "failed" if self.failed else "done" if self.completed else "ready" if self.arguments_completed else "running"
        if self.tool_name is not None:
            data["tool_name"] = self.tool_name
        arguments = "".join(self.arguments_parts)
        if arguments:
            data["arguments"] = arguments
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
    value = event.data.get("tool_name") or event.data.get("tool_id")
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
    """Format the event text that sits to the right of the timeline gutter."""
    title, description, color = _event_conversation_parts(event)
    text = Text()
    text.append(title, style=f"bold {color}" if title else color)
    if description:
        text.append(" ")
        text.append(_truncate_inline(description, 96), style=COLORS["text_dim"])
    return text


def _event_conversation_parts(event: TuiEvent) -> tuple[str, str, str]:
    data = event.data
    if event.event_type in {"llm.stream.started", "llm.stream.completed", "llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta"}:
        elapsed = _format_seconds(data.get("elapsed_ms") or event.duration_ms)
        token_count = int(data.get("token_count") or data.get("delta_count") or 0)
        suffix = "tokens" if token_count != 1 else "token"
        return f"Thought for {elapsed}", f"{token_count} {suffix} >", COLORS["text_dim"]

    if event.event_type == "llm.completed":
        response = data.get("response", {})
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        total = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
        return "Response", f"{total} tokens" if total else "", COLORS["text"]

    if event.event_type == "llm.requested":
        model = data.get("model") or "model"
        messages = data.get("messages", [])
        tools = data.get("tools", [])
        message_count = len(messages) if isinstance(messages, list | tuple) else 0
        tool_count = len(tools) if isinstance(tools, list | tuple) else 0
        return "Request", f"{model} · {message_count} messages · {tool_count} tools", COLORS["text_dim"]

    if event.event_type == "llm.failed":
        return "LLM failed", str(event.error or data.get("error") or ""), COLORS["red"]

    if event.event_type.startswith("tool."):
        return _tool_event_title(data), _tool_event_preview(data), _event_marker_color(event)

    if event.event_type == "run.started":
        meta = data.get("metadata", {})
        if isinstance(meta, dict):
            return "Run", str(meta.get("role") or meta.get("objective") or data.get("context_id") or "started"), COLORS["text"]
        return "Run", str(data.get("context_id") or "started"), COLORS["text"]

    if event.event_type == "run.completed":
        return "Run completed", f"{data.get('outcome', 'done')} · {data.get('steps', 0)} steps", COLORS["green"]

    if event.event_type == "step.started":
        return "Step", f"{event.step_number} started" if event.step_number is not None else "started", COLORS["cyan"]

    if event.event_type == "step.completed":
        trace = data.get("trace", {})
        outcome = trace.get("outcome") if isinstance(trace, dict) else data.get("outcome")
        return "Step completed", str(outcome or ""), COLORS["cyan"]

    if event.event_type.startswith("tool_selection."):
        return "Tool selection", _event_description(event), COLORS["teal"]

    return event.event_type, _event_description(event), COLORS["text"]


def _format_seconds(value: Any) -> str:
    if not isinstance(value, int | float) or value <= 0:
        return "0s"
    seconds = max(0, int(round(value / 1000)))
    return f"{seconds}s"


def _truncate_inline(value: str, max_chars: int) -> str:
    text = " ".join(_normalize_display_text(str(value)).split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _tool_event_title(data: dict[str, Any]) -> str:
    name = str(data.get("tool_name") or data.get("tool_id") or data.get("tool_call_id") or "Tool")
    display = {
        "read_file": "Read",
        "write_file": "Write",
        "shell_execute": "Bash",
        "finish": "Finish",
    }.get(name)
    return display or name.replace("_", " ").title()


def _tool_event_preview(data: dict[str, Any]) -> str:
    arguments = _tool_arguments(data)
    if arguments is None:
        return ""
    parsed = _jsonish_value(arguments)
    value = arguments if parsed is None else parsed
    if isinstance(value, dict):
        for key in ("path", "file", "command", "cmd", "query", "description"):
            if key in value:
                item = value[key]
                if isinstance(item, list | tuple):
                    return _truncate_inline(" ".join(str(part) for part in item), 96)
                return _truncate_inline(str(item), 96)
    return _truncate_inline(_compact_inline(value, max_chars=120), 96)


def _format_event_line_plain(event: TuiEvent) -> str:
    return str(_format_event_line(event)).rstrip()


def _format_event_detail_plain(event: TuiEvent) -> str:
    return _strip_rich_markup(_format_event_detail(event)).rstrip()


def _format_event_transcript(events: list[TuiEvent]) -> str:
    chunks: list[str] = []
    for event in events:
        chunks.append(_format_event_line_plain(event))
        detail = _format_event_detail_plain(event)
        if detail:
            chunks.append(detail)
        chunks.append("")
    return "\n".join(chunks).rstrip() + "\n" if chunks else ""


def _strip_rich_markup(value: str) -> str:
    return _RICH_TAG_RE.sub("", value)


def _event_scope(event: TuiEvent) -> tuple[str, str]:
    if event.event_type.startswith("run."):
        return "LOOP", COLORS["green"]
    if event.event_type.startswith("step."):
        return "STEP", COLORS["cyan"]
    if event.event_type.startswith("llm."):
        round_number = event.data.get("llm_round")
        return (f"LLM#{round_number}" if round_number else "LLM", COLORS["magenta"])
    if event.event_type.startswith("tool_selection."):
        return "TOOLSEL", COLORS["teal"]
    if event.event_type.startswith("tool."):
        return "TOOL", COLORS["orange"] if event.event_type != "tool.completed" else COLORS["green"]
    if event.event_type == "_tui_done":
        return "TUI", COLORS["text_dim"]
    return "EVENT", COLORS["text_dim"]


def _event_name(event: TuiEvent) -> str:
    mapping = {
        "run.started": "started",
        "run.completed": "completed",
        "step.started": "started",
        "step.completed": "completed",
        "llm.requested": "request",
        "llm.stream.started": "sse",
        "llm.stream.completed": "sse",
        "llm.content.delta": "sse",
        "llm.reasoning.delta": "sse",
        "llm.reasoning_context.delta": "sse",
        "llm.completed": "response",
        "llm.failed": "failed",
        "tool.started": "call",
        "tool.completed": "call",
        "tool.failed": "failed",
        "tool_selection.requested": "select",
        "tool_selection.decided": "selected",
        "tool_selection.failed": "failed",
        "_tui_done": "done",
    }
    return mapping.get(event.event_type, event.event_type)


def _event_description(event: TuiEvent) -> str:
    data = event.data
    if event.event_type == "run.started":
        meta = data.get("metadata", {})
        if isinstance(meta, dict):
            return str(meta.get("role") or meta.get("objective") or data.get("context_id") or "run started")
        return str(data.get("context_id") or "run started")
    if event.event_type == "run.completed":
        return f"{data.get('outcome', 'done')} / {data.get('steps', 0)} step(s)"
    if event.event_type == "step.started":
        return f"step {event.step_number} started" if event.step_number is not None else "step started"
    if event.event_type == "step.completed":
        trace = data.get("trace", {})
        outcome = trace.get("outcome") if isinstance(trace, dict) else data.get("outcome")
        return str(outcome or "step completed")
    if event.event_type == "llm.requested":
        messages = data.get("messages", [])
        tools = data.get("tools", [])
        message_count = len(messages) if isinstance(messages, list | tuple) else 0
        tool_count = len(tools) if isinstance(tools, list | tuple) else 0
        model = data.get("model") or "model"
        return f"{model} / {message_count} messages / {tool_count} tools"
    if event.event_type in {"llm.stream.started", "llm.stream.completed", "llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta"}:
        labels = []
        if data.get("reasoning"):
            labels.append("thinking")
        if data.get("reasoning_context"):
            labels.append("reasoning_context")
        if data.get("content"):
            labels.append("content")
        if not labels and data.get("delta_count"):
            labels.append(f"{data.get('delta_count')} chunks")
        return " + ".join(labels) if labels else "stream"
    if event.event_type == "llm.completed":
        response = data.get("response", {})
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        total = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
        finish = response.get("finish_reason") if isinstance(response, dict) else None
        return f"final response / {total} tokens / {finish or 'done'}"
    if event.event_type == "llm.failed":
        return str(event.error or data.get("error") or "llm failed")
    if event.event_type.startswith("tool."):
        name = data.get("tool_name") or data.get("tool_id") or data.get("tool_call_id") or "tool"
        if event.event_type == "tool.completed":
            return f"{name} / fn call + result"
        if event.event_type == "tool.failed":
            return f"{name} / failed"
        return f"{name} / fn call"
    if event.event_type == "tool_selection.decided":
        selected = data.get("selected_tools", [])
        excluded = data.get("excluded_tools", [])
        return f"select:{len(selected)} exclude:{len(excluded)}"
    if event.event_type == "tool_selection.requested":
        available = data.get("available_tools", [])
        return f"{len(available)} available tools" if isinstance(available, list) else "select tools"
    if event.event_type == "tool_selection.failed":
        return str(event.error or "tool selection failed")
    return event.event_type


def _event_status(event: TuiEvent) -> str:
    data_status = event.data.get("status")
    if isinstance(data_status, str) and data_status:
        return data_status
    if event.event_type.endswith(".failed"):
        return "failed"
    if event.event_type in {"run.completed", "step.completed", "llm.completed", "tool.completed", "tool_selection.decided", "_tui_done"}:
        return "done"
    if event.event_type in {"llm.stream.started", "llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta"}:
        return "streaming"
    if event.event_type.endswith(".started") or event.event_type.endswith(".requested"):
        return "running"
    return "event"


def _status_color(status: str) -> str:
    if status in {"done", "completed", "ready"}:
        return COLORS["green"]
    if status in {"running", "streaming"}:
        return COLORS["orange"]
    if status == "failed":
        return COLORS["red"]
    return COLORS["text_dim"]


def _event_marker_color(event: TuiEvent) -> str:
    if event.event_type.endswith(".failed"):
        return COLORS["red"]
    if event.event_type.startswith("tool.") and event.event_type != "tool.started":
        return COLORS["green"]
    if event.event_type == "tool.started":
        return COLORS["orange"]
    if event.event_type.startswith("llm.stream") or event.event_type in {"llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta"}:
        return COLORS["text_dim"]
    if event.event_type.startswith("llm."):
        return COLORS["text_dim"]
    if event.event_type.startswith("run.") or event.event_type.startswith("step."):
        return COLORS["text_dim"]
    return COLORS["text_dim"]


def _format_event_gutter(event: TuiEvent, *, expanded: bool, detail_height: int) -> str:
    color = _event_marker_color(event)
    line_count = detail_height + 1 if expanded else 1
    lines = [f"[{color}]●[/]"]
    lines.extend(f"[{COLORS['border']}]│[/]" for _ in range(line_count))
    return "\n".join(lines)


def _format_event_detail(event: TuiEvent) -> str:
    """Format full event detail for the detail panel."""
    lines: list[str] = []
    data = event.data

    if event.event_type == "llm.requested":
        lines.append(f"[bold {COLORS['magenta']}]─── LLM Request ───[/]")
        _append_llm_input_details(lines, data)

    elif event.event_type == "llm.completed":
        if data.get("messages") or data.get("tools"):
            lines.append(f"[bold {COLORS['magenta']}]─── LLM Input ───[/]")
            _append_llm_input_details(lines, data)

        if _has_llm_stream_details(data):
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
            else:
                tool_call_count = _response_tool_call_count(resp)
                if tool_call_count:
                    lines.append(f"[dim]tool-call response:[/] {tool_call_count} call(s), no text content")
                else:
                    lines.append("[dim]empty response:[/] no text content")
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
        delta = data.get("delta", "")
        if not _append_jsonish(lines, delta, indent="  "):
            _append_wrapped(lines, str(delta), indent="  ")
        lines.append("")

    elif event.event_type in {"llm.stream.started", "llm.stream.completed"}:
        _append_llm_stream_details(lines, data)
        lines.append("")

    elif event.event_type == "tool.started":
        _append_tool_detail_input(lines, data)
        lines.append("")

    elif event.event_type == "tool.completed":
        _append_tool_detail_input(lines, data)
        lines.append(f"[bold {COLORS['green']}]OUT[/]")
        out = data.get("output")
        if out is not None:
            value = out.value if hasattr(out, "value") else out
            if not _append_jsonish(lines, value, indent="  "):
                lines.append(f"  {value}")
        lines.append("")

    elif event.event_type == "tool.failed":
        _append_tool_detail_input(lines, data)
        lines.append(f"[bold {COLORS['red']}]OUT[/]")
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


def _response_tool_call_count(response: dict[str, Any]) -> int:
    tool_calls = response.get("tool_calls", ())
    if isinstance(tool_calls, list | tuple):
        return len(tool_calls)
    return 0


def _append_tool_detail_input(lines: list[str], data: dict[str, Any]) -> None:
    lines.append(f"[bold {COLORS['orange']}]IN[/]")
    name = data.get("tool_name") or data.get("tool_id") or data.get("tool_call_id") or "?"
    arguments = _tool_arguments(data)
    if arguments is None:
        lines.append(f"  {name}")
        return
    lines.append(f"  {name} {_compact_inline(arguments)}")


def _tool_arguments(data: dict[str, Any]) -> Any | None:
    return data.get("arguments") if "arguments" in data else data.get("input")


def _compact_inline(value: Any, *, max_chars: int = 800) -> str:
    parsed = _jsonish_value(value)
    render_value = value if parsed is None else parsed
    if isinstance(render_value, dict | list | tuple):
        rendered = json.dumps(render_value, ensure_ascii=False, default=str)
    else:
        rendered = _normalize_display_text(str(render_value))
        rendered = " ".join(rendered.split())
    if len(rendered) > max_chars:
        return rendered[: max_chars - 3] + "..."
    return rendered


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
    return any(data.get(key) for key in ("content", "reasoning", "reasoning_context"))


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
    """Adaptive-height scrollable detail area embedded inside an event item."""

    DETAIL_MAX_HEIGHT = 10
    BORDER_CHROME_LINES = 2

    DEFAULT_CSS = f"""
    EventDetailBox {{
        height: auto;
        max-height: {DETAIL_MAX_HEIGHT};
        background: {COLORS["bg_dark"]};
        border: round {COLORS["border"]};
        padding: 0 1;
        margin: 0 0 1 0;
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=True, highlight=True, wrap=True, **kwargs)

    def set_event(self, event: TuiEvent) -> None:
        detail = _format_event_detail(event)
        height = self.detail_height_for_text(detail)
        self.styles.height = height
        self.styles.max_height = self.DETAIL_MAX_HEIGHT
        self.clear()
        self.write(detail)
        self.scroll_end(animate=False)

    @classmethod
    def detail_height_for_event(cls, event: TuiEvent) -> int:
        return cls.detail_height_for_text(_format_event_detail(event))

    @classmethod
    def detail_height_for_text(cls, detail: str) -> int:
        plain = _strip_rich_markup(detail).rstrip()
        line_count = len(plain.splitlines()) if plain else 1
        return max(1 + cls.BORDER_CHROME_LINES, min(cls.DETAIL_MAX_HEIGHT, line_count + cls.BORDER_CHROME_LINES))


class EventItem(Container):
    """One event row with an inline collapsible detail box."""

    DEFAULT_CSS = f"""
    EventItem {{
        width: 100%;
        height: auto;
        min-height: 1;
        layout: horizontal;
    }}
    EventItem.-selected {{
        background: {COLORS["bg_dark"]};
    }}
    .event-gutter {{
        width: 3;
        min-width: 3;
        height: auto;
        padding: 0;
        content-align: center top;
    }}
    .event-body {{
        width: 1fr;
        height: auto;
        padding: 0;
    }}
    .event-summary {{
        height: 1;
        color: {COLORS["text"]};
        padding: 0;
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
        self.detail_height = EventDetailBox.detail_height_for_event(self.event)
        self._gutter = Label(_format_event_gutter(self.event, expanded=self.is_expanded, detail_height=self.detail_height), classes="event-gutter")
        self._summary = Label(_format_event_line(self.event), classes="event-summary")
        self._detail = EventDetailBox(classes="event-detail")

    def compose(self) -> ComposeResult:
        yield self._gutter
        with Container(classes="event-body"):
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

    def on_click(self, event: Any) -> None:
        event.stop()
        self.toggle_expanded()

    def _apply_event(self) -> None:
        self._summary.update(_format_event_line(self.event))
        self._detail.set_event(self.event)
        self.detail_height = (
            int(self._detail.styles.height.value) if self._detail.styles.height is not None else EventDetailBox.detail_height_for_event(self.event)
        )
        self._gutter.update(_format_event_gutter(self.event, expanded=self.is_expanded, detail_height=self.detail_height))

    def _apply_state(self) -> None:
        if self.is_selected:
            self.add_class("-selected")
        else:
            self.remove_class("-selected")
        self._detail.display = self.is_expanded
        self._gutter.update(_format_event_gutter(self.event, expanded=self.is_expanded, detail_height=self.detail_height))


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

    def add_event(self, event: TuiEvent, *, pinned_expanded: bool = False, expanded: bool = True, selected: bool = True) -> None:
        """Append an event item and expand it as the active event."""
        for item in self._event_items:
            item.set_selected(False)
            if not item.is_pinned_expanded:
                item.set_expanded(False)

        item = EventItem(event, expanded=expanded, selected=selected, pinned_expanded=pinned_expanded)
        self.mount(item)
        self._event_items.append(item)
        if selected:
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

    def get_selected_event(self) -> TuiEvent | None:
        return self.get_event(self._selected_index)

    def get_events(self) -> list[TuiEvent]:
        return [item.event for item in self._event_items]

    def get_item(self, index: int) -> EventItem:
        return self._event_items[index]

    @property
    def event_count(self) -> int:
        return len(self._event_items)

    def select_event(self, index: int, *, expand: bool = True) -> TuiEvent | None:
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
        if expand:
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
        ("y", "copy_detail", "Copy Detail"),
        ("Y", "copy_transcript", "Copy All"),
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
        self._llm_rounds: dict[str, int] = {}
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

        event = self._with_llm_round(event, llm_call_id)
        feed = self.query_one("#event_feed", EventFeedWidget)

        if event.event_type == "llm.requested":
            feed.add_event(event)
            return True

        if event.event_type in {"llm.completed", "llm.failed"}:
            feed.add_event(event, pinned_expanded=True)
            if event.event_type == "llm.completed":
                self._add_llm_usage(event)
            return True

        stream = self._llm_streams.get(llm_call_id)
        if stream is None:
            stream = _LlmStreamState.from_event(event)
            self._llm_streams[llm_call_id] = stream
            feed.add_event(stream.current_event or event, expanded=False)
            self._llm_stream_indices[llm_call_id] = feed.event_count - 1
        else:
            aggregate = stream.absorb(event)
            feed.update_event(self._llm_stream_indices[llm_call_id], aggregate)
            feed.scroll_end(animate=False)

        return True

    def _with_llm_round(self, event: TuiEvent, llm_call_id: str) -> TuiEvent:
        round_number = self._llm_rounds.get(llm_call_id)
        if round_number is None:
            round_number = len(self._llm_rounds) + 1
            self._llm_rounds[llm_call_id] = round_number
        data = dict(event.data)
        data["llm_round"] = round_number
        return replace(event, data=data)

    def _add_llm_usage(self, event: TuiEvent) -> None:
        response = event.data.get("response", {})
        if not isinstance(response, dict):
            return
        usage = response.get("usage", {})
        if not isinstance(usage, dict):
            return
        total_tokens = usage.get("total_tokens", 0)
        if total_tokens:
            self._total_tokens += total_tokens
            status_bar = self.query_one("#status", StatusBar)
            status_bar.tokens = self._total_tokens

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

    def action_copy_detail(self) -> None:
        """Copy the selected event detail as plain text."""
        feed = self.query_one("#event_feed", EventFeedWidget)
        event = feed.get_selected_event()
        if event is None:
            self._set_status("copy-empty")
            return
        self._copy_text(_format_event_detail_plain(event))

    def action_copy_transcript(self) -> None:
        """Copy the full visible event transcript as plain text."""
        feed = self.query_one("#event_feed", EventFeedWidget)
        text = _format_event_transcript(feed.get_events())
        if not text.strip():
            self._set_status("copy-empty")
            return
        self._copy_text(text)

    def _copy_text(self, text: str) -> None:
        path = Path(".loom/tui/last-copy.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        try:
            self.copy_to_clipboard(text)
        except BaseException:
            self._set_status(f"copy-saved:{path}")
            return
        self._set_status("copied")

    def _set_status(self, status: str) -> None:
        status_bar = self.query_one("#status", StatusBar)
        status_bar.status = status

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
