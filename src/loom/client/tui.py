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
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Static

from loom.client.budgets import parse_token_budget, snapshot_token_budget
from loom.client.projection import SessionProjection
from loom.service.contracts import ServiceError
from loom.tui.tui_app import EventFeedWidget, LoomTuiApp, LoopHeader, StatusBar
from loom.tui.tui_collector import TuiEvent


class SessionTuiApp(LoomTuiApp):
    CSS = """
    #sessions { width: 26; border-right: solid $accent; }
    #session_body { width: 1fr; }
    #question, #plan, #notice, #budget { height: auto; max-height: 6; padding: 0 1; }
    #controls { height: 3; }
    #controls Button { min-width: 7; padding: 0 1; }
    #message { height: 3; }
    """
    BINDINGS = [("ctrl+q", "quit", "Disconnect"), ("ctrl+r", "refresh_sessions", "Refresh sessions")]

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
                yield EventFeedWidget(id="event_feed")
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
            await view.extend([ListItem(Label(Text(f"{s['title']}\n{s['task']['state']}")), id=f"session_{i}") for i, s in enumerate(sessions)])
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
            history = await asyncio.to_thread(self.client.history, sid, limit=200)
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

    def _restore_streams(self):
        feed = self.query_one(EventFeedWidget)
        for key, text in self.projection.streams.items():
            llm_id, channel = key.split(":", 1)
            field = {"content": "content_parts", "reasoning": "reasoning_parts", "reasoning_context": "reasoning_context_parts"}.get(channel)
            if not field:
                continue
            event = TuiEvent(time.monotonic(), f"llm.{channel}.delta", {"llm_call_id": llm_id}, llm_call_id=llm_id)
            self._handle_event(event)
            stream = self._llm_streams[llm_id]
            setattr(stream, field, [text])
            aggregate = stream.absorb(event)
            index = self._llm_stream_indices.get(llm_id)
            if index is None:
                feed.add_event(aggregate)
                self._llm_stream_indices[llm_id] = feed.event_count - 1
            else:
                feed.update_event(index, aggregate)

    async def _clear_feed(self):
        feed = self.query_one(EventFeedWidget)
        for item in list(feed._event_items):
            await item.remove()
        feed._event_items.clear()
        feed._selected_index = -1
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
            time.monotonic(),
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
            self.message_indices[data["id"]] = feed.event_count
            feed.add_event(event)
        elif kind == "command.applied":
            cid = envelope.get("command_id")
            for index in self.message_indices.values():
                old = feed.get_event(index)
                if old and old.data.get("command_id") == cid:
                    from dataclasses import replace

                    feed.update_event(index, replace(old, data={**old.data, "state": "applied"}))
        elif "artifact" in data or kind.startswith("input."):
            feed.add_event(event)
        elif not kind.startswith(("command.", "operation.", "task.")):
            self._handle_event(event)
        if feed.event_count > 500 and not self.pruning:
            self.pruning = True
            self.call_later(self._prune_feed)

    async def _prune_feed(self):
        feed = self.query_one(EventFeedWidget)
        count = max(0, feed.event_count - 500)
        for item in feed._event_items[:count]:
            await item.remove()
        del feed._event_items[:count]
        feed._selected_index = max(-1, feed._selected_index - count)
        for mapping in (self._llm_stream_indices, self._tool_execution_indices, self.message_indices):
            remaining = {k: i - count for k, i in mapping.items() if i >= count}
            mapping.clear()
            mapping.update(remaining)
        for mapping, indices in (
            (self._llm_streams, self._llm_stream_indices),
            (self._llm_rounds, self._llm_stream_indices),
            (self._tool_executions, self._tool_execution_indices),
        ):
            for key in list(mapping):
                if key not in indices:
                    del mapping[key]
        self._decision_trace_ids.clear()
        self._presented_issue_keys.clear()
        self.pruning = False

    def _refresh_state(self):
        state = self.projection.snapshot
        task = state["task"]
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
        lines = [f"{item['status']}: {item['content']}" for item in plan.get("items", [])]
        self.query_one("#plan", Static).update(Text("\n".join(lines)))
        self.query_one("#supersede", Button).disabled = not pending or request["kind"] == "recovery"
        self.query_one("#message", Input).placeholder = "Enter a task and press Enter" if waiting else "Add guidance or answer the pending question"
        self.query_one("#pause", Button).disabled = waiting or task["state"] not in {"running", "queued", "pausing"}
        self.query_one("#resume", Button).disabled = waiting or bool(pending) or task["state"] not in {"paused", "failed"}
        self.query_one("#stop_run", Button).disabled = waiting or task["state"] in {"idle", "completed", "recovering"}
        budget = snapshot_token_budget(state)
        used = f"{budget['used']:,}" if budget["used"] is not None else "unknown"
        limit = f"{budget['limit']:,}" if budget["limit"] is not None else "unknown"
        self.query_one("#budget", Static).update(Text(f"Tokens: {used} / {limit} — /budget 20M to change"))

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
            budget = snapshot_token_budget(snapshot)
            self.projection.snapshot["token_budget"] = budget
            if budget["limit"] is not None:
                self.projection.snapshot["task"].setdefault("limits", {})["max_tokens"] = budget["limit"]
            self._refresh_state()
            self.query_one("#message", Input).value = ""
            self._notice(
                budget["notice"]
                if "notice" in budget
                else f"Token budget: {budget['used']:,} used / {budget['limit']:,} total. Press Resume to continue a paused task."
            )
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
            selected = self.query_one(EventFeedWidget).get_selected_event()
            if selected and selected.data.get("artifact"):
                try:
                    raw = await asyncio.to_thread(self.client.artifact, self.session_id, selected.data["artifact"]["sha256"])
                    value = json.loads(raw)
                    self.query_one(EventFeedWidget).add_event(TuiEvent(time.monotonic(), value["type"], value), pinned_expanded=True)
                except (OSError, ServiceError, URLError) as exc:
                    self._notice(str(exc))
        else:
            await self.send_control(kind)

    async def on_unmount(self):
        self.subscription_stop.set()
        if self.subscription:
            self.subscription.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.subscription
