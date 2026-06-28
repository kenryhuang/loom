from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("textual")

from loom.tui.tui_app import DetailPanel, LoomTuiApp, LoopHeader, TimelineWidget
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


def test_detail_panel_appends_events_and_includes_tool_result(monkeypatch):
    panel = DetailPanel()
    cleared = 0
    writes = []

    def fake_clear():
        nonlocal cleared
        cleared += 1

    monkeypatch.setattr(panel, "clear", fake_clear)
    monkeypatch.setattr(panel, "write", writes.append)

    panel.show_event(
        TuiEvent(
            timestamp=0,
            event_type="run.started",
            data={"type": "run.started", "context_id": "ctx-1"},
        )
    )
    panel.show_event(
        TuiEvent(
            timestamp=1,
            event_type="tool.completed",
            data={
                "type": "tool.completed",
                "tool_id": "search-notes",
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

    assert cleared == 0
    assert len(writes) == 2
    assert "Run Started" in writes[0]
    assert "Tool Result" in writes[1]
    assert "search-notes" in writes[1]
    assert "Live Loom smoke test" in writes[1]
    assert "real tool result" in writes[1]


def test_detail_panel_pretty_prints_json_strings(monkeypatch):
    panel = DetailPanel()
    writes = []

    monkeypatch.setattr(panel, "write", writes.append)

    panel.show_event(
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
    panel = DetailPanel()
    writes = []

    monkeypatch.setattr(panel, "write", writes.append)

    panel.show_event(
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
    panel = DetailPanel()
    writes = []
    monkeypatch.setattr(panel, "write", writes.append)

    panel.show_event(
        TuiEvent(
            timestamp=0,
            event_type="llm.content.delta",
            data={"type": "llm.content.delta", "delta": '{"partial": true}', "llm_call_id": "call-1"},
            llm_call_id="call-1",
        )
    )
    panel.show_event(
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
async def test_tui_app_aggregates_llm_stream_tokens_into_one_timeline_event(monkeypatch):
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        detail = app.query_one("#detail", DetailPanel)
        writes = []
        monkeypatch.setattr(detail, "write", writes.append)

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

        timeline = app.query_one("#timeline", TimelineWidget)
        stream_event = timeline.get_event(0)

        assert timeline.event_count == 1
        assert stream_event is not None
        assert stream_event.event_type == "llm.stream.completed"
        assert stream_event.duration_ms == 42
        assert stream_event.data["content"] == "hello world"
        assert "hello world" in writes[-1]


def test_detail_panel_renders_llm_completed_report_with_real_newlines(monkeypatch):
    panel = DetailPanel()
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

    panel.show_event(
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
