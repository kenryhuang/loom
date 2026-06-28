from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("textual")

from loom.tui.tui_app import EventDetailBox, EventFeedWidget, LoomTuiApp, LoopHeader
from loom.tui.tui_collector import TuiEvent, TuiEventCollector


@pytest.mark.asyncio
async def test_set_loop_info_before_mount_updates_header_after_mount():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    app.set_loop_info(role="counter loop", goal="count to five")

    async with app.run_test():
        header = app.query_one("#loop_header", LoopHeader)

        assert header.loop_role == "counter loop"
        assert header.loop_goal == "count to five"


@pytest.mark.asyncio
async def test_tui_app_uses_single_event_feed_with_inline_details():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        assert len(list(app.query("#event_feed"))) == 1
        assert list(app.query("#timeline")) == []
        assert list(app.query("#detail")) == []

        app._handle_event(
            TuiEvent(
                timestamp=0,
                event_type="run.started",
                data={"type": "run.started", "context_id": "ctx-1"},
            )
        )

        feed = app.query_one("#event_feed")
        assert feed.event_count == 1
        assert feed.get_selected_index() == 0


@pytest.mark.asyncio
async def test_event_feed_collapses_previous_event_and_uses_fixed_detail_height():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        app._handle_event(
            TuiEvent(
                timestamp=0,
                event_type="run.started",
                data={"type": "run.started", "context_id": "ctx-1"},
            )
        )
        app._handle_event(
            TuiEvent(
                timestamp=1,
                event_type="tool.completed",
                data={"type": "tool.completed", "tool_id": "search", "output": {"value": {"summary": "done"}}},
            )
        )

        feed = app.query_one("#event_feed")
        first_item = feed.get_item(0)
        second_item = feed.get_item(1)

        assert first_item.is_expanded is False
        assert second_item.is_expanded is True
        assert second_item.detail_height == 12


@pytest.mark.asyncio
async def test_tui_app_aggregates_tool_input_and_output_into_one_event():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        app._handle_event(
            TuiEvent(
                timestamp=0,
                event_type="tool.started",
                data={
                    "type": "tool.started",
                    "tool_id": "search",
                    "tool_call_id": "tool-call-1",
                    "input": {"query": "loom"},
                },
                tool_call_id="tool-call-1",
            )
        )
        app._handle_event(
            TuiEvent(
                timestamp=1,
                event_type="tool.completed",
                data={
                    "type": "tool.completed",
                    "tool_id": "search",
                    "tool_call_id": "tool-call-1",
                    "input": {"query": "loom"},
                    "output": {"value": {"summary": "found docs"}},
                },
                tool_call_id="tool-call-1",
                duration_ms=9,
            )
        )

        feed = app.query_one("#event_feed", EventFeedWidget)
        tool_event = feed.get_event(0)

        assert feed.event_count == 1
        assert tool_event is not None
        assert tool_event.event_type == "tool.completed"
        assert tool_event.duration_ms == 9
        assert tool_event.data["input"] == {"query": "loom"}
        assert tool_event.data["output"] == {"value": {"summary": "found docs"}}


@pytest.mark.asyncio
async def test_tui_app_keeps_tool_event_detail_expanded_after_following_events():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        app._handle_event(
            TuiEvent(
                timestamp=0,
                event_type="tool.started",
                data={
                    "type": "tool.started",
                    "tool_id": "search",
                    "tool_call_id": "tool-call-1",
                    "input": {"query": "loom"},
                },
                tool_call_id="tool-call-1",
            )
        )
        app._handle_event(
            TuiEvent(
                timestamp=1,
                event_type="tool.completed",
                data={
                    "type": "tool.completed",
                    "tool_id": "search",
                    "tool_call_id": "tool-call-1",
                    "input": {"query": "loom"},
                    "output": {"value": {"summary": "found docs"}},
                },
                tool_call_id="tool-call-1",
            )
        )
        app._handle_event(
            TuiEvent(
                timestamp=2,
                event_type="run.completed",
                data={"type": "run.completed", "outcome": "pass", "steps": 1},
            )
        )

        feed = app.query_one("#event_feed", EventFeedWidget)
        tool_item = feed.get_item(0)
        tool_event = feed.get_event(0)

        assert feed.event_count == 2
        assert tool_item.is_expanded is True
        assert tool_event is not None
        assert tool_event.data["input"] == {"query": "loom"}
        assert tool_event.data["output"] == {"value": {"summary": "found docs"}}


@pytest.mark.asyncio
async def test_tui_app_aggregates_llm_input_stream_and_response_and_keeps_it_expanded():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        for event in (
            TuiEvent(
                timestamp=0,
                event_type="llm.requested",
                data={
                    "type": "llm.requested",
                    "llm_call_id": "llm-1",
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "inspect project"}],
                    "tools": [{"function": {"name": "search", "description": "Search files"}}],
                },
                llm_call_id="llm-1",
            ),
            TuiEvent(
                timestamp=1,
                event_type="llm.stream.started",
                data={"type": "llm.stream.started", "llm_call_id": "llm-1", "model": "test-model"},
                llm_call_id="llm-1",
            ),
            TuiEvent(
                timestamp=2,
                event_type="llm.reasoning.delta",
                data={"type": "llm.reasoning.delta", "llm_call_id": "llm-1", "delta": "thinking "},
                llm_call_id="llm-1",
            ),
            TuiEvent(
                timestamp=3,
                event_type="llm.reasoning_context.delta",
                data={"type": "llm.reasoning_context.delta", "llm_call_id": "llm-1", "delta": "ctx"},
                llm_call_id="llm-1",
            ),
            TuiEvent(
                timestamp=4,
                event_type="llm.content.delta",
                data={"type": "llm.content.delta", "llm_call_id": "llm-1", "delta": "answer"},
                llm_call_id="llm-1",
            ),
            TuiEvent(
                timestamp=5,
                event_type="llm.tool_call.started",
                data={"type": "llm.tool_call.started", "llm_call_id": "llm-1", "tool_call_id": "tc-1", "tool_name": "search"},
                llm_call_id="llm-1",
                tool_call_id="tc-1",
            ),
            TuiEvent(
                timestamp=6,
                event_type="llm.tool_call.arguments.delta",
                data={
                    "type": "llm.tool_call.arguments.delta",
                    "llm_call_id": "llm-1",
                    "tool_call_id": "tc-1",
                    "tool_name": "search",
                    "delta": '{"query":"loom"}',
                },
                llm_call_id="llm-1",
                tool_call_id="tc-1",
            ),
            TuiEvent(
                timestamp=7,
                event_type="llm.stream.completed",
                data={"type": "llm.stream.completed", "llm_call_id": "llm-1", "model": "test-model"},
                llm_call_id="llm-1",
                duration_ms=17,
            ),
            TuiEvent(
                timestamp=8,
                event_type="llm.completed",
                data={
                    "type": "llm.completed",
                    "llm_call_id": "llm-1",
                    "model": "test-model",
                    "response": {
                        "content": "final answer",
                        "tool_calls": [{"id": "tc-1", "name": "search", "arguments": '{"query":"loom"}'}],
                        "usage": {"total_tokens": 42},
                        "finish_reason": "stop",
                    },
                },
                llm_call_id="llm-1",
            ),
        ):
            app._handle_event(event)

        app._handle_event(
            TuiEvent(
                timestamp=9,
                event_type="run.completed",
                data={"type": "run.completed", "outcome": "pass", "steps": 1},
            )
        )

        feed = app.query_one("#event_feed", EventFeedWidget)
        llm_event = feed.get_event(0)
        llm_item = feed.get_item(0)

        assert feed.event_count == 2
        assert llm_event is not None
        assert llm_event.event_type == "llm.completed"
        assert llm_event.data["messages"] == [{"role": "user", "content": "inspect project"}]
        assert llm_event.data["content"] == "answer"
        assert llm_event.data["reasoning"] == "thinking "
        assert llm_event.data["reasoning_context"] == "ctx"
        assert llm_event.data["tool_calls"][0]["arguments"] == '{"query":"loom"}'
        assert llm_event.data["response"]["content"] == "final answer"
        assert llm_item.is_expanded is True


def test_event_detail_box_includes_tool_result(monkeypatch):
    panel = EventDetailBox()
    cleared = 0
    writes = []

    def fake_clear():
        nonlocal cleared
        cleared += 1

    monkeypatch.setattr(panel, "clear", fake_clear)
    monkeypatch.setattr(panel, "write", writes.append)

    panel.set_event(
        TuiEvent(
            timestamp=1,
            event_type="tool.completed",
            data={
                "type": "tool.completed",
                "tool_id": "search-notes",
                "input": {"query": "loom"},
                "output": {
                    "id": "obs-1",
                    "source": "search-notes",
                    "value": {
                        "matches": [
                            {
                                "title": "Live Loom smoke test",
                                "summary": "real tool result",
                            }
                        ]
                    },
                },
            },
        )
    )

    assert cleared == 1
    assert len(writes) == 1
    assert "Tool Result" in writes[0]
    assert "search-notes" in writes[0]
    assert "[dim]input:[/]" in writes[0]
    assert '"query": "loom"' in writes[0]
    assert "[dim]output:[/]" in writes[0]
    assert "Live Loom smoke test" in writes[0]
    assert "real tool result" in writes[0]


def test_detail_panel_pretty_prints_json_strings(monkeypatch):
    panel = EventDetailBox()
    writes = []

    monkeypatch.setattr(panel, "write", writes.append)

    panel.set_event(
        TuiEvent(
            timestamp=0,
            event_type="tool.completed",
            data={
                "type": "tool.completed",
                "tool_id": "search-notes",
                "output": '{"matches":[{"title":"Live Loom smoke test","summary":"real tool result"}]}',
            },
        )
    )

    assert len(writes) == 1
    assert '  {\n    "matches": [\n      {' in writes[0]
    assert '"title": "Live Loom smoke test"' in writes[0]
    assert '"summary": "real tool result"' in writes[0]


def test_detail_panel_renders_json_string_values_with_real_newlines(monkeypatch):
    panel = EventDetailBox()
    writes = []

    monkeypatch.setattr(panel, "write", writes.append)

    panel.set_event(
        TuiEvent(
            timestamp=0,
            event_type="tool.completed",
            data={
                "type": "tool.completed",
                "tool_id": "smoke-test",
                "output": {
                    "value": {
                        "report": "first finding\n\nsecond finding",
                        "escaped": "alpha\\nbeta",
                    }
                },
            },
        )
    )

    assert re.search(r"first finding\n\n\s*second finding", writes[0])
    assert re.search(r"alpha\n\s*beta", writes[0])
    assert "first finding\\n\\nsecond finding" not in writes[0]
    assert "alpha\\nbeta" not in writes[0]


def test_detail_panel_renders_llm_stream_deltas(monkeypatch):
    panel = EventDetailBox()
    writes = []
    monkeypatch.setattr(panel, "write", writes.append)

    panel.set_event(
        TuiEvent(
            timestamp=0,
            event_type="llm.content.delta",
            data={"type": "llm.content.delta", "delta": '{"partial": true}', "llm_call_id": "call-1"},
            llm_call_id="call-1",
        )
    )
    panel.set_event(
        TuiEvent(
            timestamp=1,
            event_type="llm.tool_call.arguments.delta",
            data={
                "type": "llm.tool_call.arguments.delta",
                "delta": '{"query":"loom"}',
                "tool_name": "search",
                "tool_call_id": "tool-1",
            },
            llm_call_id="call-1",
            tool_call_id="tool-1",
        )
    )

    assert "LLM Stream" in writes[0]
    assert '"partial": true' in writes[0]
    assert "Tool Arguments" in writes[1]
    assert '"query": "loom"' in writes[1]


@pytest.mark.asyncio
async def test_tui_app_aggregates_llm_stream_tokens_into_one_timeline_event():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        for event in (
            TuiEvent(
                timestamp=0,
                event_type="llm.stream.started",
                data={"type": "llm.stream.started", "llm_call_id": "call-1", "model": "test-model"},
                llm_call_id="call-1",
            ),
            TuiEvent(
                timestamp=1,
                event_type="llm.content.delta",
                data={"type": "llm.content.delta", "llm_call_id": "call-1", "delta": "hello "},
                llm_call_id="call-1",
            ),
            TuiEvent(
                timestamp=2,
                event_type="llm.content.delta",
                data={"type": "llm.content.delta", "llm_call_id": "call-1", "delta": "world"},
                llm_call_id="call-1",
            ),
            TuiEvent(
                timestamp=3,
                event_type="llm.stream.completed",
                data={"type": "llm.stream.completed", "llm_call_id": "call-1", "model": "test-model"},
                llm_call_id="call-1",
                duration_ms=42,
            ),
        ):
            app._handle_event(event)

        feed = app.query_one("#event_feed", EventFeedWidget)
        stream_event = feed.get_event(0)
        stream_item = feed.get_item(0)

        assert feed.event_count == 1
        assert stream_event is not None
        assert stream_event.event_type == "llm.stream.completed"
        assert stream_event.duration_ms == 42
        assert stream_event.data["content"] == "hello world"
        assert stream_item.is_expanded is True


def test_detail_panel_renders_llm_completed_report_with_real_newlines(monkeypatch):
    panel = EventDetailBox()
    writes = []
    monkeypatch.setattr(panel, "write", writes.append)
    report = "# Smoke Report\n\nThe LLM made this judgment."
    content = json.dumps(
        {
            "reasoning": "Evidence is enough.",
            "action": {
                "kind": "custom",
                "description": "Write report",
                "input": {"report": report},
            },
            "alternatives": [],
            "confidence": 0.8,
        }
    )

    panel.set_event(
        TuiEvent(
            timestamp=0,
            event_type="llm.completed",
            data={
                "type": "llm.completed",
                "response": {
                    "content": content,
                    "tool_calls": [],
                    "usage": {"total_tokens": 10},
                    "finish_reason": "stop",
                },
            },
        )
    )

    assert "# Smoke Report\n\nThe LLM made this judgment." in writes[0]
    assert "\\n\\nThe LLM" not in writes[0]
