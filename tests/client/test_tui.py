from copy import deepcopy

import pytest

pytest.importorskip("textual")

from loom.client.tui import SessionTuiApp
from loom.tui.tui_app import EventFeedWidget, LoomTuiApp
from loom.tui.tui_collector import TuiEvent, TuiEventCollector


class FakeClient:
    def __init__(self):
        self.commands = []
        self.states = {
            sid: {
                "session_id": sid,
                "title": sid,
                "event_cursor": 0,
                "messages": [],
                "task": {"objective": sid, "state": "idle", "revision": 1},
                "input_request": None,
                "run": None,
            }
            for sid in ("one", "two")
        }

    def list_sessions(self):
        return list(self.states.values())

    def snapshot(self, sid):
        return deepcopy(self.states[sid])

    def history(self, sid, **_kwargs):
        return {"events": [], "next_before": None}

    def events(self, sid, after, **_kwargs):
        return iter(())

    def command(self, sid, kind, payload=None, **kwargs):
        self.commands.append({"session_id": sid, "type": kind, "payload": payload or {}, **kwargs})
        return {"state": "accepted"}


@pytest.mark.asyncio
async def test_session_switch_rejects_old_stream_and_submits_input():
    client = FakeClient()
    app = SessionTuiApp(client, "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await app.select_session("one")
        old = app.subscription_generation
        await app.submit_text("Prioritize regression")
        assert client.commands[-1]["type"] == "submit_message"
        await app.select_session("two")
        assert not app.apply_session_event({"session_id": "one", "seq": 1, "type": "message.created", "payload": {"content": "old"}}, old)
        assert app.session_id == "two"
        assert app.query_one(EventFeedWidget).event_count == 0
        await app.send_control("pause")
        assert client.commands[-1]["session_id"] == "two"
        assert client.commands[-1]["type"] == "pause"


@pytest.mark.asyncio
async def test_pending_question_uses_request_id_and_shows_applied_message():
    client = FakeClient()
    client.states["one"]["input_request"] = {"id": "q", "question": "Which module?", "kind": "clarification", "state": "pending"}
    client.states["one"]["task"]["state"] = "awaiting_input"
    app = SessionTuiApp(client, "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await app.select_session("one")
        await app.submit_text("Payments")
        assert client.commands[-1]["type"] == "answer_input"
        assert client.commands[-1]["payload"] == {"request_id": "q", "answer": "Payments"}
        app.apply_session_event(
            {
                "session_id": "one",
                "seq": 1,
                "type": "message.created",
                "payload": {"id": "m", "role": "user", "content": "Payments", "command_id": "c", "state": "accepted"},
            },
            app.subscription_generation,
        )
        app.apply_session_event({"session_id": "one", "seq": 2, "type": "command.applied", "command_id": "c", "payload": {}}, app.subscription_generation)
        assert app.projection.snapshot["messages"][0]["state"] == "applied"
        assert app.query_one(EventFeedWidget).event_count >= 1


@pytest.mark.asyncio
async def test_pending_follow_callback_cannot_override_manual_scroll():
    app = LoomTuiApp(TuiEventCollector())
    async with app.run_test(size=(100, 24)) as pilot:
        feed = app.query_one(EventFeedWidget)
        for index in range(30):
            feed.add_event(TuiEvent(index, "run.started", {"context_id": str(index)}))
        await pilot.pause()
        feed.update_event(29, TuiEvent(31, "run.started", {"context_id": "updated"}))
        manual = feed.max_scroll_y // 2
        feed.scroll_to(y=manual, animate=False, immediate=True)
        await pilot.pause()
        assert feed.scroll_y == manual
        assert not feed.follow_tail
