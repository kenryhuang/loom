"""Persistent session TUI using the existing model/tool execution feed."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError

from rich.markup import escape
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Static

from loom.client.budgets import parse_token_budget
from loom.client.projection import SessionProjection
from loom.service.contracts import LIMITS, ServiceError
from loom.tui.compact import CompactEventFeedWidget, CompactEventItem
from loom.tui.tui_app import COLORS, EventFeedWidget, LoomTuiApp, LoopHeader, StatusBar
from loom.tui.tui_collector import TuiEvent


class SessionTuiApp(LoomTuiApp):
    CSS = f"""
    #sessions {{ width: 26; border-right: solid $accent; }}
    #sessions > ListItem {{
        height: auto;
        margin: 0 1 1 1;
        padding: 0 1;
        border: round $accent 40%;
    }}
    #sessions > ListItem.-highlight {{ border: round $accent; }}
    #sessions > ListItem > Label {{
        width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis;
        background: {COLORS["bg_panel"]};
    }}
    #sessions > ListItem > Label.session-workspace {{ background: {COLORS["bg_dark"]}; }}
    #session_body {{ width: 1fr; }}
    #question, #plan, #notice, #budget {{ height: auto; max-height: 6; padding: 0 1; }}
    #controls {{ height: 3; }}
    #controls Button {{ min-width: 7; padding: 0 1; }}
    #message {{ height: 3; }}
    """
    BINDINGS = [
        Binding("ctrl+c", "quit", "Disconnect", priority=True),
        ("ctrl+q", "quit", "Disconnect"),
        ("ctrl+r", "refresh_sessions", "Refresh sessions"),
    ]

    def __init__(self, client, session_id=None):
        super().__init__(SimpleNamespace(queue=asyncio.Queue()))
        self.client = client
        self.session_id = session_id
        self.projection = None
        self.subscription_generation = 0
        self.subscription = None
        self.subscription_stop = threading.Event()
        self.session_ids = []
        self.history_before = None
        self.message_indices = {}
        self.pruning = False

    def compose(self):
        yield LoopHeader(id="loop_header")
        with Horizontal():
            yield ListView(id="sessions")
            with Vertical(id="session_body"):
                yield Static("", id="question")
                yield Static("", id="budget")
                yield Static("", id="plan")
                yield CompactEventFeedWidget(id="event_feed")
                yield Static("", id="notice")
                yield Input(placeholder="Add guidance or answer the pending question", id="message")
                with Horizontal(id="controls"):
                    for kind, label in (
                        ("pause", "Pause"),
                        ("resume", "Resume"),
                        ("stop_run", "Stop"),
                        ("supersede", "Redirect"),
                        ("older", "History"),
                        ("artifact", "Detail"),
                    ):
                        yield Button(label, id=kind)
        yield StatusBar(id="status")
        yield Footer()

    async def on_mount(self):
        await self.action_refresh_sessions()
        if self.session_id or self.session_ids:
            await self.select_session(self.session_id or self.session_ids[0])
        self.query_one("#message", Input).focus()
        self.set_interval(5, self.action_refresh_sessions)

    async def action_refresh_sessions(self):
        try:
            sessions = await asyncio.to_thread(self.client.list_sessions)
            self.session_ids = [s["session_id"] for s in sessions]
            view = self.query_one("#sessions", ListView)
            await view.clear()
            items = []
            for i, session in enumerate(sessions):
                workspace = session["task"].get("workspace") or "—"
                workspace_label = Label(Text(workspace), classes="session-workspace")
                workspace_label.tooltip = workspace
                items.append(
                    ListItem(
                        Label(Text(session["title"])),
                        workspace_label,
                        Label(Text(session["task"]["state"])),
                        id=f"session_{i}",
                    )
                )
            await view.extend(items)
        except (OSError, ServiceError, URLError) as exc:
            self._notice(str(exc))

    async def on_list_view_selected(self, event):
        index = int(event.item.id.removeprefix("session_"))
        if index < len(self.session_ids):
            await self.select_session(self.session_ids[index])

    async def select_session(self, sid):
        self.subscription_generation += 1
        generation = self.subscription_generation
        self.subscription_stop.set()
        if self.subscription and self.subscription is not asyncio.current_task():
            self.subscription.cancel()
        self.subscription_stop = threading.Event()
        try:
            snapshot = await asyncio.to_thread(self.client.snapshot, sid)
            history = await self._current_run_history(sid, snapshot, generation)
            if generation != self.subscription_generation:
                return
            self.session_id = sid
            self.projection = SessionProjection(snapshot)
            await self._clear_feed()
            self.history_before = history["next_before"]
            for event in history["events"]:
                if event["seq"] <= snapshot["event_cursor"]:
                    self._render_event(event)
            self._restore_streams()
            self._refresh_state()
            self._notice(f"Session: {sid}" + (" — enter a task to begin" if not snapshot["task"]["objective"] else ""))
            self.subscription = asyncio.create_task(self._follow(sid, generation, self.subscription_stop))
        except (ServiceError, OSError, URLError) as exc:
            self._notice(str(exc))

    async def _current_run_history(self, sid, snapshot, generation):
        history = await asyncio.to_thread(self.client.history, sid, limit=200)
        run_id = (snapshot.get("run") or {}).get("id")
        # File edits may precede the latest page; replay the current run completely.
        while run_id and history["next_before"] and generation == self.subscription_generation:
            events = history["events"]
            if any(event.get("run_id") != run_id or event["type"] == "run.started" for event in events):
                break
            before = history["next_before"]
            older = await asyncio.to_thread(self.client.history, sid, before=before, limit=200)
            if not older["events"] or older["next_before"] == before:
                break
            history = {"events": [*older["events"], *events], "next_before": older["next_before"]}
        return history

    def _restore_streams(self):
        run_id = (self.projection.snapshot.get("run") or {}).get("id", "")
        self.query_one(CompactEventFeedWidget).restore_streams(self.projection.streams, run_id)

    async def _clear_feed(self):
        feed = self.query_one(EventFeedWidget)
        for item in list(feed._event_items):
            await item.remove()
        feed._event_items.clear()
        feed._selected_index = -1
        feed.reset_presentation()
        for mapping in (
            self._llm_streams,
            self._llm_stream_indices,
            self._llm_rounds,
            self._tool_executions,
            self._tool_execution_indices,
            self._pending_plan_tool_executions,
            self.message_indices,
        ):
            mapping.clear()
        self._decision_trace_ids.clear()
        self._presented_issue_keys.clear()
        self._total_tokens = self._step_count = 0

    async def _follow(self, sid, generation, stop):
        while generation == self.subscription_generation and not stop.is_set():
            try:
                iterator = self.client.events(sid, self.projection.cursor, stop=stop)
                while not stop.is_set():
                    event = await asyncio.to_thread(next, iterator, None)
                    if event is None:
                        break
                    if not self.apply_session_event(event, generation) and generation == self.subscription_generation:
                        asyncio.create_task(self.select_session(sid))
                        return
            except (OSError, URLError, ServiceError) as exc:
                if generation == self.subscription_generation:
                    self._notice(f"Disconnected; reconnecting: {exc}")
            await asyncio.sleep(0.5)

    def apply_session_event(self, event, generation):
        if generation != self.subscription_generation or not self.projection:
            return False
        try:
            applied = self.projection.apply(event)
        except ServiceError:
            return False
        if applied:
            self._render_event(event)
            self.projection.snapshot["messages"] = self.projection.snapshot["messages"][-200:]
            self._refresh_state()
            self._notice("")
        return True

    def _render_event(self, envelope):
        kind, data = envelope["type"], envelope["payload"]
        event = TuiEvent(
            time.time(),
            kind,
            data,
            trace_id=envelope.get("trace_id"),
            llm_call_id=data.get("llm_call_id"),
            tool_call_id=data.get("tool_call_id"),
            run_id=envelope.get("run_id"),
            step_number=data.get("step_number"),
            error=str(data["error"]) if data.get("error") else None,
        )
        feed = self.query_one(EventFeedWidget)
        if kind == "message.created":
            feed.add_event(event)
            self.message_indices[data["id"]] = feed._event_items[-1]
        elif kind == "command.applied":
            cid = envelope.get("command_id")
            for item in self.message_indices.values():
                old = item.event
                if old and old.data.get("command_id") == cid:
                    from dataclasses import replace

                    item.set_event(replace(old, data={**old.data, "state": "applied"}))
        else:
            self._handle_event(event)
        if feed.record_count > 500 and not self.pruning:
            self.pruning = True
            self.call_later(self._prune_feed)

    def _handle_event(self, event):
        self._update_runtime_metrics(event)
        self.query_one(CompactEventFeedWidget).present(event)

    async def _prune_feed(self):
        feed = self.query_one(CompactEventFeedWidget)
        await feed.prune_oldest(500)
        self.message_indices = {key: item for key, item in self.message_indices.items() if item in feed._event_items}
        self._decision_trace_ids.clear()
        self._presented_issue_keys.clear()
        feed.prune_presentation()
        self.pruning = False

    def _refresh_state(self):
        state = self.projection.snapshot
        task = state["task"]
        run = state.get("run") or {}
        group = self.query_one(CompactEventFeedWidget).process_groups.get(run.get("id", ""))
        if group and task["state"] in {"paused", "failed", "awaiting_input"}:
            reason = next((row.event.data.get("reason") for row in reversed(group.records) if row.event.data.get("reason")), "")
            label = {"paused": "已暂停", "failed": "执行失败", "awaiting_input": "等待回答"}[task["state"]]
            group.set_preview(label + (" · " + reason if reason else ""))
        elif group and run.get("state") == "completed":
            group.set_preview("已完成")
        waiting = not task["objective"]
        self.query_one(StatusBar).status = "awaiting_task" if waiting else task["state"]
        header = self.query_one(LoopHeader)
        header.loop_role = escape(state["title"])
        header.loop_goal = escape(task["objective"]) if not waiting else "Enter a task to begin"
        request = state.get("input_request")
        pending = request and request["state"] == "pending"
        question = ("Recovery: " if request["kind"] == "recovery" else "Question: ") + request["question"] if pending else ""
        self.query_one("#question", Static).update(Text(question))
        plan = state.get("plan") or state.get("plan_event", {}).get("plan", {})
        items = plan.get("items", [])
        terminal = sum(item.get("status") in {"completed", "skipped"} for item in items)
        self.query_one("#plan", Static).update(Text(f"Plan · {terminal}/{len(items)} complete" if items else ""))
        self.query_one("#supersede", Button).disabled = not pending or request["kind"] == "recovery"
        self.query_one("#message", Input).placeholder = "Enter a task and press Enter" if waiting else "Add guidance or answer the pending question"
        self.query_one("#pause", Button).disabled = waiting or task["state"] not in {"running", "queued", "pausing"}
        self.query_one("#resume", Button).disabled = waiting or bool(pending) or task["state"] not in {"paused", "failed"}
        self.query_one("#stop_run", Button).disabled = waiting or task["state"] in {"idle", "completed", "recovering"}
        budget = state.get("token_budget", {})
        limit = task.get("limits", {}).get("max_tokens", LIMITS["max_tokens"])
        self.query_one("#budget", Static).update(Text(f"Tokens: {budget.get('used', 0):,} / {limit:,} — /budget 20M to change"))

    def _notice(self, content):
        self.query_one("#notice", Static).update(Text(content))

    async def submit_text(self, content, *, supersede=False):
        if not self.projection or not content.strip():
            return
        if not supersede and content.strip().split(maxsplit=1)[0] == "/budget":
            await self._budget_command(content)
            return
        request = self.projection.snapshot.get("input_request")
        if request and request["state"] == "pending":
            kind = "supersede_input" if supersede else "answer_input"
            payload = {"request_id": request["id"], "content" if supersede else "answer": content}
        else:
            kind, payload = "submit_message", {"content": content}
        try:
            await asyncio.to_thread(self.client.command, self.session_id, kind, payload)
            self.query_one("#message", Input).value = ""
            self._notice("Accepted; waiting for the execution boundary")
        except (ServiceError, OSError, URLError) as exc:
            self._notice(str(exc))

    async def _budget_command(self, content):
        sid, generation = self.session_id, self.subscription_generation
        args = content.strip().split(maxsplit=1)
        try:
            if len(args) == 2:
                count = parse_token_budget(args[1])
                await asyncio.to_thread(self.client.command, sid, "set_token_budget", {"max_tokens": count})
            snapshot = await asyncio.to_thread(self.client.snapshot, sid)
            if generation != self.subscription_generation:
                return
            budget = snapshot["token_budget"]
            self.projection.snapshot["token_budget"] = budget
            self.projection.snapshot["task"].setdefault("limits", {})["max_tokens"] = budget["limit"]
            self._refresh_state()
            self.query_one("#message", Input).value = ""
            self._notice(f"Token budget: {budget['used']:,} used / {budget['limit']:,} total. Press Resume to continue a paused task.")
        except (ValueError, ServiceError, OSError, URLError) as exc:
            if generation == self.subscription_generation:
                self._notice(str(exc))

    async def send_control(self, kind):
        if self.session_id:
            try:
                await asyncio.to_thread(self.client.command, self.session_id, kind, {})
            except (ServiceError, OSError, URLError) as exc:
                self._notice(str(exc))

    async def on_input_submitted(self, event):
        await self.submit_text(event.value)

    async def on_compact_event_item_details_requested(self, event: CompactEventItem.DetailsRequested):
        item = event.item
        generation, sid = self.subscription_generation, self.session_id
        digest = item.event.data["artifact"]["sha256"]
        try:
            raw = await asyncio.to_thread(self.client.artifact, sid, digest)
            value = json.loads(raw)
            feed = self.query_one(CompactEventFeedWidget)
            if generation != self.subscription_generation or item not in feed.all_items():
                return
            item.detail_loading = False
            item.loaded_artifact = digest
            feed.load_event_details(
                item,
                TuiEvent(
                    time.time(),
                    value["type"],
                    value,
                    run_id=item.event.run_id,
                    llm_call_id=value.get("llm_call_id"),
                    tool_call_id=value.get("tool_call_id"),
                ),
            )
        except (OSError, ServiceError, URLError, ValueError) as exc:
            if generation == self.subscription_generation:
                item.detail_loading = False
                item.detail_error = f"Could not load full details: {exc}. Collapse and expand to retry."
                item._refresh()

    async def on_button_pressed(self, event):
        kind = event.button.id
        if kind == "supersede":
            await self.submit_text(self.query_one(Input).value, supersede=True)
        elif kind == "older":
            if self.history_before:
                page = await asyncio.to_thread(self.client.history, self.session_id, before=self.history_before, limit=200)
                await self._clear_feed()
                self.history_before = page["next_before"]
                for envelope in page["events"]:
                    self._render_event(envelope)
                self._notice("Earlier history; live execution continues")
        elif kind == "artifact":
            feed = self.query_one(EventFeedWidget)
            item = feed.get_item(feed.get_selected_index())
            selected = feed.get_selected_event()
            if item and item.kind not in {"message", "result"}:
                item.toggle_expanded()
            elif selected and selected.data.get("artifact"):
                try:
                    raw = await asyncio.to_thread(self.client.artifact, self.session_id, selected.data["artifact"]["sha256"])
                    value = json.loads(raw)
                    self.query_one(EventFeedWidget).add_event(
                        TuiEvent(
                            time.time(),
                            value["type"],
                            value,
                            run_id=selected.run_id,
                            llm_call_id=value.get("llm_call_id"),
                            tool_call_id=value.get("tool_call_id"),
                        )
                    )
                except (OSError, ServiceError, URLError) as exc:
                    self._notice(str(exc))
            else:
                self.query_one(EventFeedWidget).toggle_selected_detail()
        else:
            await self.send_control(kind)

    async def on_unmount(self):
        self.subscription_stop.set()
        if self.subscription:
            self.subscription.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.subscription
