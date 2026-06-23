"""Textual TUI app for Loom loop visualization — Codex/Claude style."""

from __future__ import annotations

import asyncio
import json
import time
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
    "tool.started": {"icon": "⚙", "color": COLORS["orange"], "label": "TOOL →"},
    "tool.completed": {"icon": "⚙", "color": COLORS["green"], "label": "TOOL ←"},
    "tool.failed": {"icon": "⚙", "color": COLORS["red"], "label": "TOOL FAIL"},
    "tool_selection.requested": {"icon": "🔧", "color": COLORS["teal"], "label": "TOOLSEL →"},
    "tool_selection.decided": {"icon": "🔧", "color": COLORS["teal"], "label": "TOOLSEL ✓"},
    "tool_selection.failed": {"icon": "🔧", "color": COLORS["red"], "label": "TOOLSEL ✗"},
    "_tui_done": {"icon": "■", "color": COLORS["text_dim"], "label": "END"},
}


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

    elif event.event_type == "llm.completed":
        lines.append(f"[bold {COLORS['magenta']}]─── LLM Response ───[/]")
        resp = data.get("response", {})
        if isinstance(resp, dict):
            content = resp.get("content")
            if content:
                lines.append(f"[bold {COLORS['magenta']}]content:[/]")
                if not _append_jsonish(lines, content, indent="  "):
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
        out = data.get("output")
        if out is not None:
            lines.append("[dim]output:[/]")
            value = out.value if hasattr(out, "value") else out
            if not _append_jsonish(lines, value, indent="  "):
                lines.append(f"  {value}")
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


def _append_jsonish(lines: list[str], value: Any, *, indent: str = "", max_chars: int | None = None) -> bool:
    """Append dict/list values or JSON strings as pretty JSON."""
    parsed = _jsonish_value(value)
    if parsed is None:
        return False
    rendered = json.dumps(parsed, ensure_ascii=False, indent=2, default=str)
    if max_chars is not None:
        rendered = rendered[:max_chars]
    lines.extend(f"{indent}{line}" for line in rendered.splitlines())
    return True


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
    prefix_len = len(indent)
    available = max_width - prefix_len
    words = text.split()
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


# ─── Widgets ───────────────────────────────────────────────────────────


class TimelineWidget(VerticalScroll):
    """Scrollable timeline of events."""

    DEFAULT_CSS = f"""
    TimelineWidget {{
        background: {COLORS["bg_panel"]};
        border: tall {COLORS["border"]};
        width: 40%;
        min-width: 30;
        padding: 0 1;
    }}
    TimelineWidget:focus {{
        border: tall {COLORS["cyan"]};
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._event_labels: list[tuple[Label, TuiEvent]] = []
        self._selected_index: int = -1

    def compose(self) -> ComposeResult:
        yield Label(
            f"[bold {COLORS['cyan']}]  EVENT TIMELINE[/]",
            classes="timeline-header",
        )
        yield Static("", classes="timeline-separator")

    def add_event(self, event: TuiEvent) -> None:
        """Add an event to the timeline."""
        text = _format_event_line(event)
        label = Label(text, classes="timeline-item")
        label.styles.padding = (0, 1)
        self.mount(label)
        self._event_labels.append((label, event))
        # Auto-scroll to bottom
        self.scroll_end(animate=False)

    def get_event(self, index: int) -> TuiEvent | None:
        if 0 <= index < len(self._event_labels):
            return self._event_labels[index][1]
        return None

    @property
    def event_count(self) -> int:
        return len(self._event_labels)

    def select_event(self, index: int) -> TuiEvent | None:
        """Select an event and return it."""
        # Clear previous selection
        if 0 <= self._selected_index < len(self._event_labels):
            prev_label = self._event_labels[self._selected_index][0]
            prev_label.styles.background = "transparent"

        if 0 <= index < len(self._event_labels):
            self._selected_index = index
            label, event = self._event_labels[index]
            label.styles.background = COLORS["border"]
            return event
        return None

    def get_selected_index(self) -> int:
        return self._selected_index


class DetailPanel(RichLog):
    """Append-only panel showing loop event details."""

    DEFAULT_CSS = f"""
    DetailPanel {{
        background: {COLORS["bg_panel"]};
        border: tall {COLORS["border"]};
        width: 60%;
        min-width: 40;
        padding: 0 1;
    }}
    DetailPanel:focus {{
        border: tall {COLORS["cyan"]};
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=True, highlight=True, wrap=True, **kwargs)
        self.border_title = f"[bold {COLORS['magenta']}] LOOP EVENT DETAILS [/]"
        self._event_count = 0

    def show_event(self, event: TuiEvent) -> None:
        """Append event detail to the log."""
        detail = _format_event_detail(event)
        if self._event_count:
            detail = f"\n[dim]{'─' * 72}[/]\n{detail}"
        self.write(detail)
        self._event_count += 1


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

    #main_container {{
        layout: horizontal;
        height: 1fr;
    }}

    .timeline-header {{
        color: {COLORS["cyan"]};
        text-style: bold;
        padding: 1 0 0 0;
    }}

    .timeline-separator {{
        height: 1;
        background: {COLORS["border"]};
    }}

    .timeline-item {{
        color: {COLORS["text"]};
    }}
    .timeline-item:hover {{
        background: {COLORS["border"]};
    }}
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("g", "scroll_top", "Top"),
        ("G", "scroll_bottom", "Bottom"),
        ("tab", "focus_next", "Next Panel"),
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

    def compose(self) -> ComposeResult:
        yield LoopHeader(id="loop_header")
        with Container(id="main_container"):
            yield TimelineWidget(id="timeline")
            yield DetailPanel(id="detail")
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

        # Add to timeline
        timeline = self.query_one("#timeline", TimelineWidget)
        timeline.add_event(event)

        # Auto-select latest event
        idx = timeline.event_count - 1
        selected = timeline.select_event(idx)
        if selected:
            detail = self.query_one("#detail", DetailPanel)
            detail.show_event(selected)

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

    def action_cursor_down(self) -> None:
        """Move selection down in timeline."""
        timeline = self.query_one("#timeline", TimelineWidget)
        idx = timeline.get_selected_index()
        if idx < timeline.event_count - 1:
            timeline.select_event(idx + 1)

    def action_cursor_up(self) -> None:
        """Move selection up in timeline."""
        timeline = self.query_one("#timeline", TimelineWidget)
        idx = timeline.get_selected_index()
        if idx > 0:
            timeline.select_event(idx - 1)

    def action_scroll_top(self) -> None:
        """Scroll to top of timeline."""
        timeline = self.query_one("#timeline", TimelineWidget)
        if timeline.event_count > 0:
            timeline.select_event(0)
        timeline.scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        """Scroll to bottom of timeline."""
        timeline = self.query_one("#timeline", TimelineWidget)
        if timeline.event_count > 0:
            timeline.select_event(timeline.event_count - 1)
        timeline.scroll_end(animate=False)

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
