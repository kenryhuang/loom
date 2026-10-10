"""Folded execution processes with expandable event rows and final results."""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import replace

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Footer, Label, Markdown, Static

from loom.observability.result_format import render_result_markdown
from loom.tui.acceptance import ROUND_EVENTS, AcceptancePanel, round_details, round_summary
from loom.tui.event_details import event_content, readable_event_details
from loom.tui.event_summary import llm_response_text, summarize_event
from loom.tui.tui_app import COLORS, EventFeedWidget, LoomTuiApp, LoopHeader, StatusBar, _event_tool_execution_key, _LlmStreamState, _ToolExecutionState
from loom.tui.tui_collector import TuiEvent, _flatten


def thought_text(event: TuiEvent) -> str:
    data = event.data
    response = _flatten(data.get("response")) or {}
    parts = []
    for field in ("reasoning", "reasoning_context"):
        value = data.get(field) or response.get(field)
        if value and value not in parts:
            parts.append(str(value))
    if parts:
        return "\n\n".join(parts)
    content = data.get("content") or response.get("content", "")
    if str(content).lstrip().startswith(("{", "[")):
        return ""
    return llm_response_text(replace(event, data={"response": {"content": content}})) or ""


def tool_details(event: TuiEvent) -> str:
    data = event.data
    sections = []
    for label, value in (
        ("Input", data.get("input", data.get("arguments"))),
        ("Output", data.get("output")),
        ("Error", data.get("error") or event.error),
        ("Reason", data.get("reason")),
    ):
        if value is None:
            continue
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                value = json.loads(value)
        text = value if isinstance(value, str) else json.dumps(_flatten(value), ensure_ascii=False, indent=2)
        sections.append(f"{label}\n{text}")
    return "\n\n".join(sections) or "Waiting for tool arguments…"


def first_sentence(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").strip().split())
    text = re.sub(r"^[#>*\-\s]+", "", text)
    match = re.search(r"[。！？]|[.!?](?=\s|$)", text)
    if match:
        text = text[: match.end()]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def output_value(event: TuiEvent) -> dict:
    value = event.data.get("output", {})
    while isinstance(value, dict) and isinstance(value.get("value"), dict):
        value = value["value"]
    return value if isinstance(value, dict) else {}


def tool_failed(event: TuiEvent) -> bool:
    value = output_value(event)
    exit_code = value.get("exit_code")
    return (
        event.event_type in {"tool.failed", "tool.interrupted"}
        or value.get("accepted") is False
        or bool(value.get("code"))
        or value.get("timed_out") is True
        or (isinstance(exit_code, int) and exit_code != 0)
    )


def tool_summary(event: TuiEvent) -> str:
    data = event.data
    name = str(data.get("tool_name") or data.get("tool_id") or "tool")
    arguments = data.get("input", data.get("arguments", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {}
    target = ""
    if isinstance(arguments, dict):
        target = arguments.get("path") or arguments.get("command") or arguments.get("query") or ""
        if isinstance(target, list):
            target = " ".join(map(str, target))
    value = output_value(event)
    exit_code = value.get("exit_code")
    failed = tool_failed(event)
    text = f"Tool call ({name})"
    if target:
        text += "  " + first_sentence(target, 120)
    if failed:
        error = data.get("error") or event.error or value.get("message") or data.get("reason")
        error = error or ("Timed out" if value.get("timed_out") else f"exit {exit_code}" if exit_code else "Failed")
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "Failed"
        text += " · " + first_sentence(error)
    return text


class CompactEventItem(Container):
    """A small progress row, or an expanded Markdown result."""

    DEFAULT_CSS = f"""
    CompactEventItem {{ height: auto; margin: 0; padding: 0 1; }}
    CompactEventItem.-selected {{ background: {COLORS["bg_dark"]}; }}
    .compact-summary {{ width: 1fr; height: 1; margin: 0; text-wrap: nowrap; text-overflow: ellipsis; }}
    CompactEventItem.-expandable > .compact-summary:hover {{ background: {COLORS["bg_panel"]}; }}
    .compact-detail-scroll {{ height: auto; max-height: 12; margin: 0 0 1 1;
        background: {COLORS["bg_dark"]}; border-left: solid {COLORS["border"]}; padding: 0 1; }}
    .compact-details {{ height: auto; color: {COLORS["text"]}; }}
    CompactEventItem Button.process-toggle {{ height: 1; min-height: 1; min-width: 1; width: 1fr;
        text-align: left; content-align: left middle; text-wrap: nowrap; text-overflow: ellipsis;
        border: none !important; background: transparent; color: {COLORS["text"]}; padding: 0; }}
    .process-scroll {{ height: auto; max-height: 24; margin: 0 0 1 1;
        border-left: solid {COLORS["border"]}; padding: 0 1; }}
    /* Fractional terminal heights round to alternating gaps between events. */
    .process-scroll > CompactEventItem {{ min-height: 2; }}
    .result-body {{ height: auto; padding: 0; margin: 0 0 1 0; }}
    .modified-files {{ height: auto; padding: 0 0 1 1; color: {COLORS["teal"]}; }}
    CompactEventItem.-result {{ margin: 1 0; border-top: solid {COLORS["border"]}; padding: 1; }}
    """

    class DetailsRequested(Message):
        def __init__(self, item: CompactEventItem):
            super().__init__()
            self.item = item

    def __init__(self, event: TuiEvent, *, selected: bool = False, feed=None, group=None):
        super().__init__()
        self.event = event
        self.feed = feed
        self.group = group
        self.records = []
        self.kind = event.data.get("view", "summary")
        self.is_selected = selected
        self.is_expanded = self.kind == "result"
        self.is_pinned_expanded = self.is_expanded
        self._summary = Label(Text(event.data.get("summary", "")), classes="compact-summary")
        self._toggle = Button("", classes="process-toggle")
        self._process_scroll = VerticalScroll(classes="process-scroll")
        self._markdown = Markdown(render_result_markdown(_flatten(event.data.get("content", ""))), classes="result-body") if self.kind == "result" else None
        self._files = Static(Text(), classes="modified-files")
        self._details = Static(Text(), classes="compact-details")
        self._details_scroll = VerticalScroll(self._details, classes="compact-detail-scroll")
        self.detail_loading = False
        self.loaded_artifact = None
        self.detail_error = ""
        self.set_class(self.kind != "process", "-expandable")
        self.set_class(self.kind == "result", "-result")

    def compose(self) -> ComposeResult:
        if self.kind == "process":
            yield self._toggle
            yield self._process_scroll
        else:
            yield self._summary
            if self.kind not in {"result", "message"}:
                yield self._details_scroll
            if self._markdown is not None:
                yield self._markdown
                yield self._files

    def on_mount(self) -> None:
        if self.kind == "process" and self.records:
            self._process_scroll.mount(*self.records)
        self._refresh()
        if self.is_expanded:
            self._request_details()

    def set_event(self, event: TuiEvent) -> None:
        old_content = self.event.data.get("content")
        self.event = event
        self._refresh()
        if self.is_expanded and not self.detail_error:
            self._request_details()
        if self._markdown is not None and old_content != event.data.get("content"):
            self._markdown.update(render_result_markdown(_flatten(event.data.get("content", ""))))

    def set_selected(self, selected: bool) -> None:
        self.is_selected = selected
        self.set_class(selected, "-selected")

    def set_expanded(self, expanded: bool) -> None:
        if self.kind != "message":
            self.is_expanded = expanded
            if not expanded and self.kind == "process" and self.feed.selected_item in self.records:
                self.feed.select_item(self, expand=False)
            self._refresh()
            if expanded:
                self._request_details()

    def _request_details(self) -> None:
        if self.kind in {"process", "result", "message"}:
            return
        artifact = self.event.data.get("artifact")
        if artifact and not self.detail_loading and artifact["sha256"] != self.loaded_artifact:
            self.detail_loading = True
            self.detail_error = ""
            self._refresh()
            self.post_message(self.DetailsRequested(self))

    def toggle_expanded(self) -> None:
        self.set_expanded(not self.is_expanded)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.feed.select_item(self, expand=False)
        self.toggle_expanded()

    def on_click(self, event) -> None:
        # Markdown links, selections and code blocks must not collapse the result.
        event.stop()
        self.feed.select_item(self, expand=False)
        if self.kind != "process" and event.widget in (self, self._summary):
            self.feed.focus()
            self.toggle_expanded()

    def _refresh(self) -> None:
        self.set_selected(self.is_selected)
        data = self.event.data
        if self.kind == "process":
            count = str(data["count"])
            if len(self.records) < data["count"]:
                count = f"{len(self.records)}/{count}"
            self._toggle.label = Text(f"{'▼' if self.is_expanded else '▶'} 过程 · {count} events · {data.get('preview', '')}")
            self._process_scroll.display = self.is_expanded
        else:
            style = COLORS["red"] if data.get("failed") else COLORS["text"]
            summary = str(data.get("summary", ""))
            if self.kind != "message":
                summary = f"{'▼' if self.is_expanded else '▶'} {summary}"
            if self.kind not in {"result", "message"}:
                self._details_scroll.display = self.is_expanded
                if self.is_expanded:
                    if self.kind == "thought":
                        detail = thought_text(self.event)
                    elif self.kind == "tool":
                        detail = tool_details(self.event)
                    elif self.kind == "acceptance":
                        detail = round_details(data["acceptance"])
                    else:
                        detail = data.get("event_details") or readable_event_details(self.event.event_type, _flatten(data))
                    if self.detail_loading:
                        detail += "\n\nLoading full details…"
                    elif self.detail_error:
                        detail += "\n\n" + self.detail_error
                    self._details.update(Text(detail))
            self._summary.update(Text(summary, style=style))
            if self._markdown is not None:
                self._markdown.display = self._files.display = self.is_expanded
                files = data.get("modified_files", [])
                self._files.update(Text(f"Modified files · {len(files)}\n" + "\n".join(f"  {path}" for path in files) if files else "Modified files · 0"))

    def add_record(self, item: CompactEventItem) -> None:
        self.records.append(item)
        if self.is_mounted:
            self._process_scroll.mount(item)
        self.set_event(replace(self.event, data={**self.event.data, "count": self.event.data["count"] + 1}))

    def set_preview(self, preview: str) -> None:
        self.set_event(replace(self.event, data={**self.event.data, "preview": preview}))


class CompactEventFeedWidget(EventFeedWidget):
    """Present session events using the same reducer for live and replayed data."""

    def __init__(self, *, session_mode: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.session_mode = session_mode
        self.reset_presentation()

    def compose(self) -> ComposeResult:
        yield Label(Text("PROGRESS", style=f"bold {COLORS['cyan']}"), classes="event-feed-header")

    def reset_presentation(self) -> None:
        self.current_run = ""
        self.selected_item = None
        self.process_groups = {}
        self.streams = {}
        self.thought_items = {}
        self.tools = {}
        self.tool_items = {}
        self.modified_files = {}
        self.result_items = {}
        self.acceptance_items = {}

    def all_items(self) -> list[CompactEventItem]:
        return [row for item in self._event_items for row in (item, *item.records)]

    def visible_items(self) -> list[CompactEventItem]:
        return [row for item in self._event_items for row in ((item, *item.records) if item.is_expanded else (item,))]

    @property
    def event_count(self) -> int:
        return len(self.visible_items())

    @property
    def record_count(self) -> int:
        return len(self.all_items())

    def get_item(self, index: int) -> CompactEventItem | None:
        items = self.visible_items()
        return items[index] if 0 <= index < len(items) else None

    def get_event(self, index: int) -> TuiEvent | None:
        item = self.get_item(index)
        return item.event if item else None

    def get_events(self) -> list[TuiEvent]:
        return [item.event for item in self.all_items()]

    def get_selected_event(self) -> TuiEvent | None:
        return self.selected_item.event if self.selected_item else None

    def get_selected_index(self) -> int:
        items = self.visible_items()
        return items.index(self.selected_item) if self.selected_item in items else -1

    def update_event(self, index: int, event: TuiEvent) -> None:
        item = self.get_item(index)
        if item:
            item.set_event(event)

    def _append(self, event: TuiEvent, *, group: CompactEventItem | None = None) -> CompactEventItem:
        following = self.follow_tail
        item = CompactEventItem(event, feed=self, group=group)
        if group:
            group.add_record(item)
        else:
            self.mount(item)
            self._event_items.append(item)
        if following:
            self.select_item(group if group and not group.is_expanded else item, expand=False)
            self.call_after_refresh(self._scroll_tail_if_following)
        return item

    def _process_group(self, event: TuiEvent) -> CompactEventItem:
        run = event.run_id or self.current_run
        group = self.process_groups.get(run)
        if group is None or group not in self._event_items:
            group = self._append(replace(event, event_type="process.group", data={"view": "process", "count": 0, "preview": ""}))
            self.process_groups[run] = group
        return group

    def add_event(self, event: TuiEvent, **_kwargs) -> None:
        self.present(event)

    def load_event_details(self, item: CompactEventItem, event: TuiEvent) -> None:
        preview = item.group.event.data["preview"] if item.group else None
        if item.kind == "thought":
            key = (event.run_id or self.current_run, event.llm_call_id or event.data.get("llm_call_id"))
            stream = self.streams[key]
            stream.absorb(event)
            self._present_thought(stream, key)
        elif item.kind not in {"tool", "result", "message"}:
            item.set_event(replace(item.event, data={**event.data, "view": item.kind, "summary": item.event.data["summary"],
                "event_details": readable_event_details(event.event_type, _flatten(event.data))}))
        else:
            self.present(event)
        if item.group and preview is not None:
            item.group.set_preview(preview)

    def select_event(self, index: int, *, expand: bool = True) -> TuiEvent | None:
        item = self.get_item(index)
        if item is None:
            return None
        return self.select_item(item, expand=expand)

    def select_item(self, item: CompactEventItem, *, expand: bool = True) -> TuiEvent:
        if self.selected_item:
            self.selected_item.set_selected(False)
        self.selected_item = item
        item.set_selected(True)
        if expand:
            item.set_expanded(True)
        return item.event

    def toggle_selected_detail(self) -> None:
        if self.selected_item:
            self.selected_item.toggle_expanded()

    async def prune_oldest(self, limit: int) -> None:
        while self.record_count > limit and self._event_items:
            item = self._event_items[0]
            excess = self.record_count - limit
            removed = item.records.pop(0) if len(item.records) >= excess else self._event_items.pop(0)
            await removed.remove()
            if item in self._event_items:
                item._refresh()
        if self.selected_item not in self.all_items():
            self.selected_item = None
        self.prune_presentation()

    def prune_presentation(self) -> None:
        retained = self.all_items()
        for items, states in ((self.thought_items, self.streams), (self.tool_items, self.tools)):
            for key in list(items):
                if items[key] not in retained:
                    del items[key]
                    states.pop(key, None)
        self.process_groups = {run: item for run, item in self.process_groups.items() if item in self._event_items}
        self.result_items = {run: item for run, item in self.result_items.items() if item in self._event_items}
        self.acceptance_items = {key: item for key, item in self.acceptance_items.items() if item in retained}
        retained_runs = {item.event.run_id for item in self._event_items} | {self.current_run}
        self.modified_files = {run: files for run, files in self.modified_files.items() if run in retained_runs}

    def _process_event(self, event: TuiEvent) -> None:
        group = self._process_group(event)
        summary = summarize_event(event)
        detail = summary.description if summary else first_sentence(event.data.get("reason") or event.data.get("state") or "")
        title, sections = event_content(event.event_type, _flatten(event.data))
        if title and sections:
            detail = sections[0][1]
        if event.event_type == "artifact.created" and event.data.get("artifact", {}).get("kind") in {"verification", "verification_evidence"}:
            detail = ""
        line = (title or event.event_type) + (" · " + first_sentence(detail) if detail else "")
        self._append(replace(event, data={**event.data, "view": "event", "summary": line,
            "event_details": readable_event_details(event.event_type, _flatten(event.data))}), group=group)
        if event.event_type == "llm.requested":
            group.set_preview("Thought: 思考中…")
        elif event.event_type == "run.completed" or event.data.get("state") == "completed":
            group.set_preview("已完成")
        elif not group.event.data["preview"].startswith(("Thought:", "Tool call (")) or event.data.get("state") in {
            "paused",
            "failed",
            "awaiting_input",
            "stopped",
        }:
            group.set_preview(line)

    def _present_result(self, event: TuiEvent, content: object) -> None:
        run = event.run_id or self.current_run
        group = self.process_groups.get(run)
        if group:
            group.set_preview("已完成")
        result = replace(
            event, data={**event.data, "view": "result", "summary": "Result", "content": content, "modified_files": sorted(self.modified_files.get(run, set()))}
        )
        previous = self.result_items.get(run)
        if not self.session_mode and previous in self._event_items and (event.event_type == "run.result" or previous.event.data.get("content") == content):
            previous.set_event(result)
            if event.event_type == "run.result":
                previous.set_expanded(True)
            if self.follow_tail:
                self.select_item(previous, expand=False)
                self.call_after_refresh(self._scroll_tail_if_following)
        else:
            self.result_items[run] = self._append(result)

    def _present_thought(self, stream: _LlmStreamState, key: tuple[str, str]) -> None:
        aggregate = stream.current_event
        data = aggregate.data
        text = thought_text(aggregate)
        if not text:
            return
        thought = replace(aggregate, data={**data, "view": "thought", "summary": "Thought: " + first_sentence(text)})
        item = self.thought_items.get(key)
        group = self._process_group(aggregate)
        if item is None or item not in group.records:
            self.thought_items[key] = self._append(thought, group=group)
        else:
            item.set_event(thought)
        group.set_preview(thought.data["summary"])

    def restore_streams(self, saved: dict[str, str], run: str = "") -> None:
        for saved_key, text in saved.items():
            llm_id, channel = saved_key.split(":", 1)
            field = {"content": "content_parts", "reasoning": "reasoning_parts", "reasoning_context": "reasoning_context_parts"}.get(channel)
            if not field or not text:
                continue
            key = (run or self.current_run, llm_id)
            stream = self.streams.get(key)
            if stream is None:
                stream = _LlmStreamState.from_event(TuiEvent(time.time(), "llm.stream.started", {"llm_call_id": llm_id}, llm_call_id=llm_id, run_id=run))
                self.streams[key] = stream
            setattr(stream, field, [text])
            stream.stream_started = True
            stream.current_event = stream._to_event()
            self._present_thought(stream, key)

    def present(self, event: TuiEvent) -> None:
        kind, data = event.event_type, event.data
        run = event.run_id or self.current_run
        if kind in ROUND_EVENTS and data.get("acceptance"):
            value = data["acceptance"]
            key = (run, value.get("goal_digest"), value.get("revision"), value.get("attempts"))
            summary, failed = round_summary(value)
            aggregate = replace(event, data={**data, "view": "acceptance", "summary": summary, "failed": failed})
            group = self._process_group(event)
            item = self.acceptance_items.get(key)
            was_failed = bool(item and item.event.data.get("failed"))
            if item is None or item not in group.records:
                item = self._append(aggregate, group=group)
                self.acceptance_items[key] = item
            else:
                item.set_event(aggregate)
            if failed and not was_failed:
                item.set_expanded(True)
                group.set_expanded(True)
            group.set_preview(summary)
            return
        if kind == "run.started":
            self.current_run = run
        if kind == "run.result" and not self.session_mode:
            content = data.get("content")
            if isinstance(content, str) and content.strip() or isinstance(content, dict | list):
                self._present_result(event, content)
            return
        if kind == "message.created":
            result = data.get("role") == "assistant"
            if result:
                self._present_result(event, data.get("content", ""))
            else:
                self._append(replace(event, data={**data, "view": "message", "summary": "You: " + first_sentence(data.get("content"))}))
            return
        if kind in {"llm.content.delta", "llm.reasoning.delta", "llm.reasoning_context.delta", "llm.stream.started", "llm.stream.completed", "llm.completed"}:
            llm_id = event.llm_call_id or data.get("llm_call_id")
            if llm_id:
                key = (run, llm_id)
                stream = self.streams.get(key)
                if stream is None:
                    stream = _LlmStreamState.from_event(event)
                    self.streams[key] = stream
                else:
                    stream.absorb(event)
                self._present_thought(stream, key)
            if not kind.endswith(".delta"):
                self._process_event(event)
            if kind == "llm.completed" and not self.session_mode and data.get("usage_role") != "verification":
                content = llm_response_text(event)
                if content:
                    self._present_result(event, content)
            return
        if kind.startswith("tool.") or kind.startswith("llm.tool_call."):
            key = (run, _event_tool_execution_key(event))
            tool = self.tools.get(key)
            if tool is None:
                tool = _ToolExecutionState.from_event(event)
                self.tools[key] = tool
            else:
                tool.absorb(event)
            aggregate = tool.current_event
            name = aggregate.data.get("tool_name") or aggregate.data.get("tool_id")
            if kind == "tool.interrupted":
                aggregate = replace(aggregate, event_type=kind, data={**aggregate.data, "reason": data.get("reason")})
            summary = tool_summary(aggregate)
            aggregate = replace(aggregate, data={**aggregate.data, "view": "tool", "summary": summary, "failed": tool_failed(aggregate)})
            item = self.tool_items.get(key)
            group = self._process_group(event)
            if item is None or item not in group.records:
                self.tool_items[key] = self._append(aggregate, group=group)
            else:
                item.set_event(aggregate)
            group.set_preview(aggregate.data["summary"])
            if name in {"edit_file", "write_file"} and kind == "tool.completed" and not aggregate.data["failed"]:
                value = output_value(aggregate)
                arguments = aggregate.data.get("input", {})
                path = value.get("path") or (arguments.get("path") if isinstance(arguments, dict) else None)
                if path:
                    self.modified_files.setdefault(run, set()).add(str(path))
                    result = self.result_items.get(run)
                    if result:
                        result.set_event(replace(result.event, data={**result.event.data, "modified_files": sorted(self.modified_files[run])}))
            if name == "finish" and kind == "tool.completed" and not self.session_mode:
                report = output_value(aggregate).get("report")
                if isinstance(report, str) and report.strip():
                    self._present_result(event, report)
            return
        summary = summarize_event(event)
        important = kind.endswith(".failed") or data.get("state") in {"failed", "paused", "awaiting_input", "stopped"}
        important = important or kind == "input.requested" or (summary is not None and summary.color_role == "red")
        if important:
            error = data.get("error") or event.error or data.get("reason") or data.get("question")
            if isinstance(error, dict):
                error = error.get("message")
            text = (summary.description if summary else "") or first_sentence(error or data.get("message") or data.get("state"))
            group = self._process_group(event)
            row = self._append(
                replace(
                    event,
                    data={
                        **data,
                        "view": "summary",
                        "summary": (summary.title if summary else kind) + ": " + text,
                        "failed": kind.endswith(".failed") or data.get("state") == "failed",
                    },
                ),
                group=group,
            )
            group.set_preview(row.event.data["summary"])
        else:
            self._process_event(event)


class CompactLoomTuiApp(LoomTuiApp):
    """Default loop and job view; persistent sessions supply authoritative messages."""

    def compose(self) -> ComposeResult:
        yield LoopHeader(id="loop_header")
        yield AcceptancePanel(id="acceptance", show_pending=False)
        yield CompactEventFeedWidget(id="event_feed", session_mode=False)
        yield StatusBar(id="status")
        yield Footer()

    def _handle_event(self, event: TuiEvent) -> None:
        if event.event_type == "_tui_done":
            super()._handle_event(event)
            return
        self._update_runtime_metrics(event)
        self.query_one(AcceptancePanel).apply_event(event)
        self.query_one(CompactEventFeedWidget).present(event)
