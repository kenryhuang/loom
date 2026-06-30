from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("textual")

from loom.tui.tui_app import EventDetailBox, EventFeedWidget, LoomTuiApp, LoopHeader, _format_event_detail_plain, _format_event_line
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


def test_event_line_uses_conversation_timeline_text_without_marker():
    line = str(
        _format_event_line(
            TuiEvent(
                timestamp=0,
                event_type="llm.completed",
                data={
                    "type": "llm.completed",
                    "llm_call_id": "llm-1",
                    "llm_round": 2,
                    "response": {"usage": {"total_tokens": 42}, "finish_reason": "stop"},
                },
                step_number=3,
                llm_call_id="llm-1",
            )
        )
    )

    assert not line.startswith("●")
    assert "Response" in line
    assert "42 tokens" in line


def test_event_detail_omits_trace_metadata():
    event = TuiEvent(
        timestamp=0,
        event_type="tool.completed",
        data={
            "type": "tool.completed",
            "tool_id": "search",
            "arguments": {"query": "loom"},
            "output": {"value": {"summary": "done"}},
        },
        run_id="run-1",
        loop_id="loop-1",
        trace_id="trace-1",
        step_number=7,
        duration_ms=123,
    )

    detail = _format_event_detail_plain(event)

    assert "type:" not in detail
    assert "run_id:" not in detail
    assert "loop_id:" not in detail
    assert "trace_id:" not in detail
    assert "step:" not in detail
    assert "duration:" not in detail


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
async def test_event_item_uses_timeline_gutter_and_body_aligned_detail():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test() as pilot:
        app._handle_event(
            TuiEvent(
                timestamp=0,
                event_type="tool.completed",
                data={"type": "tool.completed", "tool_id": "search", "input": {"query": "loom"}, "output": {"value": "ok"}},
            )
        )
        await pilot.pause()

        item = app.query_one("#event_feed", EventFeedWidget).get_item(0)

        assert len(list(item.query(".event-gutter"))) == 1
        assert len(list(item.query(".event-body"))) == 1
        assert len(list(item.query(".event-summary"))) == 1
        assert len(list(item.query(".event-detail"))) == 1


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
async def test_tui_app_displays_llm_round_as_request_sse_tool_and_response_rows():
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
                event_type="tool.started",
                data={
                    "type": "tool.started",
                    "tool_call_id": "tc-1",
                    "tool_id": "search",
                    "input": {"query": "loom"},
                },
                tool_call_id="tc-1",
            ),
            TuiEvent(
                timestamp=8,
                event_type="tool.completed",
                data={
                    "type": "tool.completed",
                    "tool_call_id": "tc-1",
                    "tool_id": "search",
                    "input": {"query": "loom"},
                    "output": {"value": {"summary": "found docs"}},
                },
                tool_call_id="tc-1",
            ),
            TuiEvent(
                timestamp=9,
                event_type="llm.stream.completed",
                data={"type": "llm.stream.completed", "llm_call_id": "llm-1", "model": "test-model"},
                llm_call_id="llm-1",
                duration_ms=17,
            ),
            TuiEvent(
                timestamp=10,
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
                timestamp=11,
                event_type="run.completed",
                data={"type": "run.completed", "outcome": "pass", "steps": 1},
            )
        )

        feed = app.query_one("#event_feed", EventFeedWidget)
        request_event = feed.get_event(0)
        sse_event = feed.get_event(1)
        tool_event = feed.get_event(2)
        response_event = feed.get_event(3)
        response_item = feed.get_item(3)

        assert feed.event_count == 5
        assert request_event is not None
        assert request_event.event_type == "llm.requested"
        assert request_event.data["llm_round"] == 1
        assert request_event.data["messages"] == [{"role": "user", "content": "inspect project"}]
        assert sse_event is not None
        assert sse_event.event_type == "llm.stream.completed"
        assert sse_event.data["content"] == "answer"
        assert sse_event.data["reasoning"] == "thinking "
        assert sse_event.data["reasoning_context"] == "ctx"
        assert tool_event is not None
        assert tool_event.event_type == "tool.completed"
        assert tool_event.data["tool_name"] == "search"
        assert tool_event.data["arguments"] == '{"query":"loom"}'
        assert response_event is not None
        assert response_event.event_type == "llm.completed"
        assert response_event.data["llm_round"] == 1
        assert response_event.data["response"]["content"] == "final answer"
        assert feed.get_item(1).is_expanded is False
        assert response_item.is_expanded is True


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
    assert "●" not in writes[0]
    assert "┌─" not in writes[0]
    assert "└─" not in writes[0]
    assert "IN" in writes[0]
    assert "OUT" in writes[0]
    assert "search-notes" in writes[0]
    assert "[dim]input:[/]" not in writes[0]
    assert '"query": "loom"' in writes[0]
    assert "Live Loom smoke test" in writes[0]
    assert "real tool result" in writes[0]


@pytest.mark.asyncio
async def test_tui_app_copies_selected_detail_as_plain_text(monkeypatch):
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

    async with app.run_test():
        app._handle_event(
            TuiEvent(
                timestamp=0,
                event_type="tool.completed",
                data={
                    "type": "tool.completed",
                    "tool_id": "search-notes",
                    "input": {"query": "loom"},
                    "output": {"value": {"summary": "found docs"}},
                },
            )
        )

        app.action_copy_detail()

    assert len(copied) == 1
    assert "IN" in copied[0]
    assert "OUT" in copied[0]
    assert '"query": "loom"' in copied[0]
    assert "input:" not in copied[0]
    assert "[dim]" not in copied[0]
    assert "[bold" not in copied[0]


@pytest.mark.asyncio
async def test_tui_app_copies_full_transcript_as_plain_text(monkeypatch):
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

    async with app.run_test():
        app._handle_event(
            TuiEvent(
                timestamp=0,
                event_type="run.started",
                data={"type": "run.started", "metadata": {"role": "tester"}},
            )
        )
        app._handle_event(
            TuiEvent(
                timestamp=1,
                event_type="run.completed",
                data={"type": "run.completed", "outcome": "pass", "steps": 1},
            )
        )

        app.action_copy_transcript()

    assert len(copied) == 1
    assert "Run tester" in copied[0]
    assert "Run completed pass" in copied[0]
    assert "Run Complete" in copied[0]
    assert "[dim]" not in copied[0]


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


def test_detail_panel_renders_llm_content_delta(monkeypatch):
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

    assert "LLM Stream" not in writes[0]
    assert '"partial": true' in writes[0]


def test_llm_stream_detail_starts_with_stream_content_and_does_not_render_headers_or_tool_calls(monkeypatch):
    panel = EventDetailBox()
    writes = []
    monkeypatch.setattr(panel, "write", writes.append)

    panel.set_event(
        TuiEvent(
            timestamp=0,
            event_type="llm.stream.started",
            data={
                "type": "llm.stream.started",
                "llm_call_id": "call-1",
                "model": "test-model",
                "reasoning": "thinking through evidence",
                "reasoning_context": "context window",
                "tool_calls": [{"id": "tool-1", "name": "search", "arguments": '{"query":"loom"}'}],
                "content": "final visible answer",
            },
            llm_call_id="call-1",
        )
    )

    detail = writes[0]
    assert detail.startswith("[bold #7aa2f7]thinking:")
    assert detail.index("thinking through evidence") < detail.index("final visible answer")
    assert detail.index("context window") < detail.index("final visible answer")
    assert "LLM Stream" not in detail
    assert "model:" not in detail
    assert "status:" not in detail
    assert "chunks:" not in detail
    assert "tool_calls" not in detail
    assert '"query": "loom"' not in detail


def test_llm_response_detail_does_not_render_tool_calls(monkeypatch):
    panel = EventDetailBox()
    writes = []
    monkeypatch.setattr(panel, "write", writes.append)

    panel.set_event(
        TuiEvent(
            timestamp=0,
            event_type="llm.completed",
            data={
                "type": "llm.completed",
                "response": {
                    "content": "final answer",
                    "tool_calls": [{"id": "tool-1", "name": "search", "arguments": '{"query":"loom"}'}],
                    "usage": {"total_tokens": 10},
                    "finish_reason": "stop",
                },
            },
        )
    )

    detail = writes[0]
    assert "final answer" in detail
    assert "tool_calls" not in detail
    assert '"query": "loom"' not in detail


@pytest.mark.asyncio
async def test_tui_app_aggregates_llm_stream_tokens_into_one_sse_row():
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
        assert stream_item.is_expanded is False


@pytest.mark.asyncio
async def test_tui_app_stream_detail_can_be_opened_after_default_collapse():
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
                event_type="llm.reasoning.delta",
                data={"type": "llm.reasoning.delta", "llm_call_id": "call-1", "delta": "thinking live"},
                llm_call_id="call-1",
            ),
        ):
            app._handle_event(event)

        feed = app.query_one("#event_feed", EventFeedWidget)
        stream_item = feed.get_item(0)

        assert stream_item.is_expanded is False

        feed.toggle_selected_detail()

        assert stream_item.is_expanded is True
        assert "thinking live" in _format_event_detail_plain(stream_item.event)


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
