from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("textual")

from loom.tui.tui_app import EventDetailBox, EventFeedWidget, LoomTuiApp, LoopHeader, _format_event_detail, _format_event_detail_plain, _format_event_line
from loom.tui.tui_collector import TuiEvent, TuiEventCollector


def _plan_event(event_type="plan.updated", revision=4):
    return TuiEvent(
        timestamp=revision,
        event_type=event_type,
        data={
            "type": event_type,
            "plan_id": "plan_test",
            "revision": revision,
            "explanation": "Adjusted after inspection",
            "plan": {
                "plan_id": "plan_test",
                "phase": "executing",
                "reason": "The task has dependent steps",
                "revision": revision,
                "items": [
                    {"id": "step_1", "content": "Inspect", "status": "completed", "note": None},
                    {"id": "step_2", "content": "Review", "status": "completed", "note": None},
                    {"id": "step_3", "content": "Implement", "status": "in_progress", "note": None},
                    {"id": "step_4", "content": "Verify", "status": "pending", "note": None},
                    {"id": "step_5", "content": "Publish", "status": "skipped", "note": "not required"},
                ],
            },
        },
    )


def test_plan_event_formats_as_checklist_summary_and_detail():
    event = _plan_event()

    line = str(_format_event_line(event))
    detail = _format_event_detail_plain(event)

    assert "Plan" in line
    assert "executing" in line
    assert "3/5 terminal" in line
    assert "✓ step_1" in detail
    assert "→ step_3" in detail
    assert "○ step_4" in detail
    assert "⊘ step_5" in detail
    assert "not required" in detail


def test_submitted_plan_line_identifies_transition_and_revision():
    event = _plan_event("plan.submitted", 1)
    event.data["plan"]["items"] = [
        {"id": "step_1", "content": "Inspect", "status": "pending", "note": None},
        {"id": "step_2", "content": "Implement", "status": "pending", "note": None},
    ]

    line = str(_format_event_line(event))
    detail = _format_event_detail_plain(event)

    assert "Plan submitted" in line
    assert "revision 1" in line
    assert "0/2 terminal" in line
    assert "○ step_1  Inspect" in detail
    assert "○ step_2  Implement" in detail


def test_updated_plan_detail_keeps_reason_and_latest_explanation():
    detail = _format_event_detail_plain(_plan_event("plan.updated", 2))

    assert "reason: The task has dependent steps" in detail
    assert "update: Adjusted after inspection" in detail


@pytest.mark.asyncio
async def test_plan_update_preserves_manual_scroll_and_selection():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test(size=(100, 24)) as pilot:
        app._handle_event(_plan_event("plan.submitted", 1))
        for index in range(30):
            app._handle_event(
                TuiEvent(
                    timestamp=index + 2,
                    event_type="run.started",
                    data={"type": "run.started", "context_id": f"ctx-{index}"},
                )
            )
        await pilot.pause()

        feed = app.query_one("#event_feed", EventFeedWidget)
        feed.scroll_end(animate=False)
        await pilot.pause()
        assert feed.max_scroll_y > 0
        feed.select_event(10)
        feed.scroll_to(y=feed.max_scroll_y // 2, animate=False, immediate=True)
        await pilot.pause()
        manual_y = feed.scroll_y
        selected_index = feed.get_selected_index()

        app._handle_event(_plan_event("plan.updated", 2))
        await pilot.pause()

        assert feed.get_selected_index() == selected_index
        assert feed.scroll_y == manual_y


@pytest.mark.asyncio
async def test_new_event_preserves_manual_scroll_and_reviewed_event():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(30):
            app._handle_event(
                TuiEvent(
                    timestamp=index,
                    event_type="run.started",
                    data={"type": "run.started", "context_id": f"ctx-{index}"},
                )
            )
        await pilot.pause()

        feed = app.query_one("#event_feed", EventFeedWidget)
        feed.select_event(10)
        feed.scroll_to(y=feed.max_scroll_y // 2, animate=False, immediate=True)
        await pilot.pause()
        manual_y = feed.scroll_y

        app._handle_event(
            TuiEvent(
                timestamp=31,
                event_type="run.completed",
                data={"type": "run.completed", "outcome": "pass", "steps": 1},
            )
        )
        await pilot.pause()

        assert feed.get_selected_index() == 10
        assert feed.scroll_y == manual_y
        assert feed.get_item(10).is_expanded is True


@pytest.mark.asyncio
async def test_event_feed_resumes_following_after_user_returns_to_bottom():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(30):
            app._handle_event(
                TuiEvent(
                    timestamp=index,
                    event_type="run.started",
                    data={"type": "run.started", "context_id": f"ctx-{index}"},
                )
            )
        await pilot.pause()

        feed = app.query_one("#event_feed", EventFeedWidget)
        feed.scroll_to(y=feed.max_scroll_y // 2, animate=False, immediate=True)
        await pilot.pause()
        assert feed.follow_tail is False

        feed.scroll_end(animate=False)
        await pilot.pause()
        assert feed.follow_tail is True

        app._handle_event(
            TuiEvent(
                timestamp=31,
                event_type="run.completed",
                data={"type": "run.completed", "outcome": "pass", "steps": 1},
            )
        )
        await pilot.wait_for_scheduled_animations()
        assert feed.is_vertical_scroll_end
        assert feed.get_selected_index() == feed.event_count - 1


@pytest.mark.asyncio
async def test_keyboard_up_and_top_pause_following_until_bottom():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(8):
            app._handle_event(
                TuiEvent(
                    timestamp=index,
                    event_type="run.started",
                    data={"type": "run.started", "context_id": f"ctx-{index}"},
                )
            )
        await pilot.pause()
        feed = app.query_one("#event_feed", EventFeedWidget)
        assert feed.follow_tail is True

        await pilot.press("k")
        assert feed.follow_tail is False
        assert feed.get_selected_index() == 6

        await pilot.press("G")
        assert feed.follow_tail is True
        assert feed.get_selected_index() == 7

        await pilot.press("g")
        assert feed.follow_tail is False
        assert feed.get_selected_index() == 0

        for _index in range(7):
            await pilot.press("j")
        assert feed.get_selected_index() == 7
        assert feed.follow_tail is True


@pytest.mark.asyncio
async def test_llm_stream_growth_preserves_manual_scroll_position():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(30):
            app._handle_event(
                TuiEvent(
                    timestamp=index,
                    event_type="run.started",
                    data={"type": "run.started", "context_id": f"ctx-{index}"},
                )
            )
        app._handle_event(
            TuiEvent(
                timestamp=31,
                event_type="llm.stream.started",
                data={"type": "llm.stream.started", "llm_call_id": "llm-stream", "model": "test-model"},
                llm_call_id="llm-stream",
            )
        )
        await pilot.pause()

        feed = app.query_one("#event_feed", EventFeedWidget)
        feed.select_event(10)
        feed.scroll_to(y=feed.max_scroll_y // 2, animate=False, immediate=True)
        await pilot.pause()
        manual_y = feed.scroll_y

        app._handle_event(
            TuiEvent(
                timestamp=32,
                event_type="llm.content.delta",
                data={"type": "llm.content.delta", "llm_call_id": "llm-stream", "delta": "new streamed content"},
                llm_call_id="llm-stream",
            )
        )
        await pilot.pause()

        assert feed.follow_tail is False
        assert feed.get_selected_index() == 10
        assert feed.scroll_y == manual_y


@pytest.mark.asyncio
async def test_tui_preserves_each_plan_revision_as_a_timeline_node():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        entered = _plan_event("plan.entered", 0)
        submitted = _plan_event("plan.submitted", 1)
        updated = _plan_event("plan.updated", 2)
        completed = _plan_event("plan.completed", 3)
        submitted.data["plan"]["items"][0]["status"] = "pending"
        updated.data["plan"]["items"][0]["status"] = "in_progress"
        completed.data["plan"]["items"][0]["status"] = "completed"

        for event in (entered, submitted, updated, completed):
            app._handle_event(event)

        feed = app.query_one("#event_feed", EventFeedWidget)
        plan_events = [event for event in feed.get_events() if event.event_type.startswith("plan.")]

        assert [event.event_type for event in plan_events] == [
            "plan.entered",
            "plan.submitted",
            "plan.updated",
            "plan.completed",
        ]
        assert [event.data["plan"]["revision"] for event in plan_events] == [0, 1, 2, 3]
        assert [event.data["plan"]["items"][0]["status"] for event in plan_events] == [
            "completed",
            "pending",
            "in_progress",
            "completed",
        ]


@pytest.mark.asyncio
async def test_plan_lifecycle_rows_keep_their_chronological_positions():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        events = (
            TuiEvent(0, "run.started", {"type": "run.started"}),
            _plan_event("plan.entered", 0),
            TuiEvent(
                2,
                "llm.requested",
                {"type": "llm.requested", "llm_call_id": "llm-1"},
                llm_call_id="llm-1",
            ),
            _plan_event("plan.submitted", 1),
            TuiEvent(4, "run.completed", {"type": "run.completed", "outcome": "pass"}),
            _plan_event("plan.updated", 2),
            _plan_event("plan.completed", 3),
        )

        for event in events:
            app._handle_event(event)

        feed = app.query_one("#event_feed", EventFeedWidget)
        assert [event.event_type for event in feed.get_events()] == [
            "run.started",
            "plan.entered",
            "plan.submitted",
            "run.completed",
            "plan.updated",
            "plan.completed",
        ]


@pytest.mark.asyncio
async def test_tui_suppresses_successful_plan_tools_and_keeps_rejections():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        app._handle_event(_plan_event("plan.updated", 2))
        feed = app.query_one("#event_feed", EventFeedWidget)

        app._handle_event(
            TuiEvent(
                timestamp=3,
                event_type="tool.started",
                data={"type": "tool.started", "tool_id": "update_plan", "tool_call_id": "accepted"},
                tool_call_id="accepted",
            )
        )
        app._handle_event(
            TuiEvent(
                timestamp=4,
                event_type="tool.completed",
                data={
                    "type": "tool.completed",
                    "tool_id": "update_plan",
                    "tool_call_id": "accepted",
                    "output": {"value": {"accepted": True}},
                },
                tool_call_id="accepted",
            )
        )
        assert feed.event_count == 1

        app._handle_event(
            TuiEvent(
                timestamp=5,
                event_type="tool.started",
                data={"type": "tool.started", "tool_id": "update_plan", "tool_call_id": "rejected"},
                tool_call_id="rejected",
            )
        )
        app._handle_event(
            TuiEvent(
                timestamp=6,
                event_type="tool.completed",
                data={
                    "type": "tool.completed",
                    "tool_id": "update_plan",
                    "tool_call_id": "rejected",
                    "output": {"value": {"accepted": False, "code": "PLAN_PHASE_INVALID"}},
                },
                tool_call_id="rejected",
            )
        )

        assert feed.event_count == 2
        assert feed.get_event(1).data["output"]["value"]["accepted"] is False


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
                    "response": {"content": "Task complete.", "usage": {"total_tokens": 42}, "finish_reason": "stop"},
                },
                step_number=3,
                llm_call_id="llm-1",
            )
        )
    )

    assert not line.startswith("●")
    assert line == "Answer Task complete."


def test_thinking_event_line_uses_elapsed_time_and_token_count_only():
    event = TuiEvent(
        timestamp=0,
        event_type="llm.stream.completed",
        data={
            "type": "llm.stream.completed",
            "elapsed_ms": 3410,
            "token_count": 2,
            "reasoning": "Inspect the project before editing.",
        },
        duration_ms=3410,
    )

    assert str(_format_event_line(event)) == "Thought for 3s 2 tokens >"


def test_thinking_event_line_uses_singular_token():
    event = TuiEvent(
        timestamp=0,
        event_type="llm.stream.started",
        data={"type": "llm.stream.started", "elapsed_ms": 1000, "delta_count": 1, "content": "hidden"},
    )

    assert str(_format_event_line(event)) == "Thought for 1s 1 token >"


@pytest.mark.asyncio
async def test_tui_curates_redundant_lifecycle_events_into_progress_rows():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        events = (
            TuiEvent(0, "run.started", {"type": "run.started", "context_id": "ctx-1"}),
            TuiEvent(
                1,
                "llm.requested",
                {"type": "llm.requested", "llm_call_id": "llm-1", "model": "test-model", "messages": [], "tools": []},
                llm_call_id="llm-1",
            ),
            TuiEvent(
                2,
                "llm.stream.started",
                {"type": "llm.stream.started", "llm_call_id": "llm-1", "model": "test-model"},
                llm_call_id="llm-1",
            ),
            TuiEvent(
                3,
                "llm.reasoning.delta",
                {"type": "llm.reasoning.delta", "llm_call_id": "llm-1", "delta": "Inspect the project before editing."},
                llm_call_id="llm-1",
            ),
            TuiEvent(
                4,
                "llm.stream.completed",
                {"type": "llm.stream.completed", "llm_call_id": "llm-1", "model": "test-model"},
                llm_call_id="llm-1",
                duration_ms=25,
            ),
            TuiEvent(
                5,
                "llm.completed",
                {
                    "type": "llm.completed",
                    "llm_call_id": "llm-1",
                    "response": {
                        "content": "",
                        "tool_calls": [{"name": "shell_execute", "arguments": {"command": ["pytest", "-q"]}}],
                        "usage": {"total_tokens": 42},
                    },
                },
                llm_call_id="llm-1",
            ),
            TuiEvent(
                6,
                "decision.recorded",
                {
                    "type": "decision.recorded",
                    "decision": {
                        "action": {"kind": "tool", "target": "run_command", "description": "Run the full test suite"},
                        "reasoning": "Establish a clean verification baseline.",
                        "confidence": 0.96,
                    },
                },
                trace_id="trace-1",
            ),
            TuiEvent(
                7,
                "action.started",
                {"type": "action.started", "action": {"description": "Run the full test suite", "target": "run_command"}},
                trace_id="trace-1",
            ),
            TuiEvent(
                8,
                "tool.started",
                {
                    "type": "tool.started",
                    "tool_id": "shell_execute",
                    "tool_call_id": "tool-1",
                    "input": {"command": ["pytest", "-q"]},
                },
                tool_call_id="tool-1",
            ),
            TuiEvent(
                9,
                "tool.completed",
                {
                    "type": "tool.completed",
                    "tool_id": "shell_execute",
                    "tool_call_id": "tool-1",
                    "input": {"command": ["pytest", "-q"]},
                    "output": {
                        "source": "shell_execute",
                        "value": {
                            "exit_code": 0,
                            "stdout": "43 passed, 8 skipped in 3.41s\n",
                            "stderr": "",
                            "duration_ms": 3410,
                            "timed_out": False,
                        },
                    },
                },
                tool_call_id="tool-1",
            ),
            TuiEvent(
                10,
                "observation.recorded",
                {
                    "type": "observation.recorded",
                    "observation": {"source": "shell_execute", "value": {"exit_code": 0}},
                },
                trace_id="trace-1",
            ),
            TuiEvent(
                11,
                "action.completed",
                {"type": "action.completed", "action": {"description": "Run the full test suite"}, "outcome": "pass"},
                trace_id="trace-1",
            ),
            TuiEvent(
                12,
                "action.recorded",
                {"type": "action.recorded", "action": {"description": "Run the full test suite"}},
                trace_id="trace-1",
            ),
            TuiEvent(
                13,
                "step.completed",
                {"type": "step.completed", "trace": {"outcome": "pass"}},
                trace_id="trace-1",
            ),
        )

        for event in events:
            app._handle_event(event)

        feed = app.query_one("#event_feed", EventFeedWidget)

        assert [event.event_type for event in feed.get_events()] == [
            "run.started",
            "llm.stream.completed",
            "decision.recorded",
            "tool.completed",
            "step.completed",
        ]
        assert str(_format_event_line(feed.get_event(1))) == "Thought for 0s 1 token >"
        assert str(_format_event_line(feed.get_event(2))) == "Decision Run the full test suite"
        assert str(_format_event_line(feed.get_event(3))) == "Bash Passed · exit 0 · 43 passed, 8 skipped · 3.41s"
        assert app.query_one("#status").tokens == 42


@pytest.mark.asyncio
async def test_tui_keeps_guard_observation_and_final_answer():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        app._handle_event(
            TuiEvent(
                0,
                "observation.recorded",
                {
                    "type": "observation.recorded",
                    "observation": {
                        "source": "planning.guard",
                        "value": {
                            "accepted": False,
                            "code": "PLAN_UPDATE_REQUIRED",
                            "message": "Update the complete plan checklist",
                        },
                    },
                },
            )
        )
        app._handle_event(
            TuiEvent(
                1,
                "llm.completed",
                {
                    "type": "llm.completed",
                    "llm_call_id": "llm-final",
                    "response": {"content": "Task complete. All tests pass.", "tool_calls": [], "usage": {"total_tokens": 12}},
                },
                llm_call_id="llm-final",
            )
        )

        feed = app.query_one("#event_feed", EventFeedWidget)

        assert [str(_format_event_line(event)) for event in feed.get_events()] == [
            "Plan blocked PLAN_UPDATE_REQUIRED · Update the complete plan checklist",
            "Answer Task complete. All tests pass.",
        ]
        assert app.query_one("#status").tokens == 12


@pytest.mark.asyncio
async def test_tui_keeps_failed_action_without_a_correlated_decision():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        app._handle_event(
            TuiEvent(
                0,
                "action.completed",
                {"type": "action.completed", "action": {"description": "Validate output"}, "outcome": "failed"},
                trace_id="trace-without-decision",
            )
        )

        event = app.query_one("#event_feed", EventFeedWidget).get_event(0)

        assert event is not None
        assert str(_format_event_line(event)) == "Action completed Validate output · failed"


@pytest.mark.asyncio
async def test_tui_hides_empty_thinking_and_unstructured_fallback_actions():
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        for event in (
            TuiEvent(
                0,
                "llm.stream.started",
                {"type": "llm.stream.started", "llm_call_id": "llm-empty"},
                llm_call_id="llm-empty",
            ),
            TuiEvent(
                1,
                "llm.stream.completed",
                {"type": "llm.stream.completed", "llm_call_id": "llm-empty"},
                llm_call_id="llm-empty",
            ),
            TuiEvent(
                2,
                "action.started",
                {"type": "action.started", "action": {"kind": "custom", "description": "Use unstructured LLM response"}},
                trace_id="trace-fallback",
            ),
            TuiEvent(
                3,
                "action.completed",
                {
                    "type": "action.completed",
                    "action": {"kind": "custom", "description": "Use unstructured LLM response"},
                    "outcome": "pass",
                },
                trace_id="trace-fallback",
            ),
        ):
            app._handle_event(event)

        assert app.query_one("#event_feed", EventFeedWidget).event_count == 0


@pytest.mark.asyncio
async def test_collected_trace_replays_as_semantic_timeline(tmp_path):
    trace_path = tmp_path / "representative-run.jsonl"
    raw_events = [
        {"type": "run.started", "run_id": "run-1", "context_id": "ctx-1"},
        {
            "type": "plan.updated",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "plan": {
                "plan_id": "plan-1",
                "phase": "executing",
                "revision": 2,
                "items": [
                    {"id": "step-1", "content": "Run tests", "status": "in_progress"},
                    {"id": "step-2", "content": "Fix defect", "status": "pending"},
                ],
            },
            "explanation": "Starting verification",
        },
        {
            "type": "decision.recorded",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "decision": {
                "action": {"kind": "tool", "target": "run_command", "description": "Run the full test suite"},
                "reasoning": "Verify the current baseline.",
                "confidence": 0.95,
            },
        },
        {
            "type": "action.started",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "action": {"kind": "tool", "target": "run_command", "description": "Run the full test suite"},
        },
        {
            "type": "tool.started",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "tool_call_id": "tool-1",
            "tool_id": "shell_execute",
            "input": {"command": ["pytest", "-q"], "cwd": "."},
        },
        {
            "type": "tool.completed",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "tool_call_id": "tool-1",
            "tool_id": "shell_execute",
            "input": {"command": ["pytest", "-q"], "cwd": "."},
            "output": {
                "source": "shell_execute",
                "value": {
                    "exit_code": 0,
                    "stdout": "43 passed, 8 skipped in 3.41s\n",
                    "stderr": "",
                    "duration_ms": 3410,
                    "timed_out": False,
                },
            },
        },
        {
            "type": "observation.recorded",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "observation": {"source": "shell_execute", "value": {"exit_code": 0}},
        },
        {
            "type": "action.completed",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "action": {"description": "Run the full test suite"},
            "outcome": "pass",
        },
        {
            "type": "action.recorded",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "action": {"description": "Run the full test suite"},
        },
        {
            "type": "tool.started",
            "run_id": "run-1",
            "trace_id": "trace-2",
            "tool_call_id": "tool-2",
            "tool_id": "write_file",
            "input": {"path": "docs/reliability-report.md", "content": "report"},
        },
        {
            "type": "tool.completed",
            "run_id": "run-1",
            "trace_id": "trace-2",
            "tool_call_id": "tool-2",
            "tool_id": "write_file",
            "input": {"path": "docs/reliability-report.md", "content": "report"},
            "output": {
                "source": "planning.guard",
                "value": {
                    "accepted": False,
                    "code": "PLAN_UPDATE_REQUIRED",
                    "message": "Update the complete plan checklist",
                },
            },
        },
        {
            "type": "observation.recorded",
            "run_id": "run-1",
            "trace_id": "trace-2",
            "observation": {
                "source": "planning.guard",
                "value": {
                    "accepted": False,
                    "code": "PLAN_UPDATE_REQUIRED",
                    "message": "Update the complete plan checklist",
                },
            },
        },
        {
            "type": "llm.completed",
            "run_id": "run-1",
            "trace_id": "trace-3",
            "llm_call_id": "llm-final",
            "response": {"content": "Task complete. All tests pass.", "tool_calls": [], "usage": {"total_tokens": 22}},
        },
        {"type": "run.completed", "run_id": "run-1", "outcome": "pass", "steps": 1},
    ]
    trace_path.write_text("\n".join(json.dumps({"type": "event", "eventType": event["type"], "payload": event}) for event in raw_events), encoding="utf-8")
    collector = TuiEventCollector()
    app = LoomTuiApp(collector)

    async with app.run_test():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            await collector.emit(record["payload"])
        app._poll_events()

        feed = app.query_one("#event_feed", EventFeedWidget)
        lines = [str(_format_event_line(event)) for event in feed.get_events()]

        assert [event.event_type for event in feed.get_events()] == [
            "run.started",
            "plan.updated",
            "decision.recorded",
            "tool.completed",
            "tool.completed",
            "llm.completed",
            "run.completed",
        ]
        assert "Bash Passed · exit 0 · 43 passed, 8 skipped · 3.41s" in lines
        assert "Write blocked PLAN_UPDATE_REQUIRED · Update the complete plan checklist" in lines
        assert "Plan blocked PLAN_UPDATE_REQUIRED · Update the complete plan checklist" not in lines
        assert "Answer Task complete. All tests pass." in lines
        assert all("decision.recorded decision.recorded" not in line for line in lines)
        assert all("action." not in line for line in lines)
        bash_line = next(line for line in lines if line.startswith("Bash "))
        assert "pytest" not in bash_line


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


def test_decision_detail_prioritizes_semantic_fields():
    event = TuiEvent(
        timestamp=0,
        event_type="decision.recorded",
        data={
            "type": "decision.recorded",
            "decision": {
                "action": {
                    "kind": "tool",
                    "description": "List project structure",
                    "target": "run_command",
                    "input": {"command": ["ls", "-la"]},
                },
                "reasoning": "The first plan item requires discovery.",
                "confidence": 0.95,
                "alternatives": [
                    {"kind": "tool", "description": "Use find for a broader view", "target": "run_command"},
                ],
            },
        },
        run_id="run-1",
        trace_id="trace-1",
        step_number=1,
    )

    detail = _format_event_detail_plain(event)

    assert detail.startswith("action: List project structure")
    assert "─── Decision ───" not in detail
    assert "action: List project structure" in detail
    assert "kind: tool" in detail
    assert "target: run_command" in detail
    assert "reasoning:" in detail
    assert "The first plan item requires discovery." in detail
    assert "confidence: 0.95" in detail
    assert "alternatives:" in detail
    assert "Use find for a broader view" in detail
    assert "run_id" not in detail
    assert "trace_id" not in detail


def test_actionable_observation_detail_prioritizes_guard_fields():
    event = TuiEvent(
        timestamp=0,
        event_type="observation.recorded",
        data={
            "type": "observation.recorded",
            "observation": {
                "id": "obs-1",
                "source": "planning.guard",
                "value": {
                    "accepted": False,
                    "code": "PLAN_UPDATE_REQUIRED",
                    "message": "Update the complete plan checklist",
                    "plan": {"phase": "executing"},
                },
            },
        },
        run_id="run-1",
        trace_id="trace-1",
    )

    detail = _format_event_detail_plain(event)

    assert "Observation" in detail
    assert "source: planning.guard" in detail
    assert "code: PLAN_UPDATE_REQUIRED" in detail
    assert "message: Update the complete plan checklist" in detail
    assert '"phase": "executing"' in detail
    assert "run_id" not in detail
    assert "trace_id" not in detail


def test_completed_tool_detail_retains_full_structured_output():
    event = TuiEvent(
        timestamp=0,
        event_type="tool.completed",
        data={
            "type": "tool.completed",
            "tool_id": "shell_execute",
            "input": {"command": ["pytest", "-q"]},
            "output": {
                "source": "shell_execute",
                "value": {"exit_code": 1, "stdout": "one failed", "stderr": "assertion failed", "duration_ms": 250},
            },
        },
    )

    detail = _format_event_detail_plain(event)

    assert '"command":' in detail
    assert '"pytest"' in detail
    assert '"stdout": "one failed"' in detail
    assert '"stderr": "assertion failed"' in detail


@pytest.mark.asyncio
async def test_event_feed_collapses_previous_event_and_uses_adaptive_detail_height():
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
                data={"type": "tool.completed", "tool_id": "search", "output": {"value": "done"}},
            )
        )

        feed = app.query_one("#event_feed")
        first_item = feed.get_item(0)
        second_item = feed.get_item(1)

        assert first_item.is_expanded is False
        assert second_item.is_expanded is True
        assert second_item.detail_height < EventDetailBox.DETAIL_MAX_HEIGHT


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


def test_event_detail_box_height_adapts_to_content_with_ten_line_cap(monkeypatch):
    panel = EventDetailBox()
    writes = []
    monkeypatch.setattr(panel, "write", writes.append)

    panel.set_event(
        TuiEvent(
            timestamp=0,
            event_type="tool.completed",
            data={"type": "tool.completed", "tool_id": "short", "input": {"query": "loom"}, "output": {"value": "ok"}},
        )
    )

    short_height = panel.styles.height.value
    assert short_height < EventDetailBox.DETAIL_MAX_HEIGHT

    panel.set_event(
        TuiEvent(
            timestamp=1,
            event_type="tool.completed",
            data={
                "type": "tool.completed",
                "tool_id": "long",
                "input": {"query": "loom"},
                "output": {"value": {"lines": [f"line-{index}" for index in range(20)]}},
            },
        )
    )

    assert panel.styles.height.value == EventDetailBox.DETAIL_MAX_HEIGHT


def test_response_detail_box_keeps_room_for_short_content_and_border(monkeypatch):
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
                    "content": "",
                    "tool_calls": [{"id": "tool-1", "name": "read_file", "arguments": '{"path":"README.md"}'}],
                    "finish_reason": "tool_calls",
                },
            },
        )
    )

    assert panel.styles.height.value >= EventDetailBox.BORDER_CHROME_LINES + 2
    assert "tool-call response" in writes[0]
    assert "finish_reason" in writes[0]


def test_event_detail_box_uses_round_border():
    assert "border: round" in EventDetailBox.DEFAULT_CSS


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
        sse_event = feed.get_event(0)
        tool_event = feed.get_event(1)
        completed_event = feed.get_event(2)

        assert feed.event_count == 3
        assert sse_event is not None
        assert sse_event.event_type == "llm.stream.completed"
        assert sse_event.data["llm_round"] == 1
        assert sse_event.data["content"] == "answer"
        assert sse_event.data["reasoning"] == "thinking "
        assert sse_event.data["reasoning_context"] == "ctx"
        assert tool_event is not None
        assert tool_event.event_type == "tool.completed"
        assert tool_event.data["tool_name"] == "search"
        assert tool_event.data["arguments"] == '{"query":"loom"}'
        assert completed_event is not None
        assert completed_event.event_type == "run.completed"
        assert app.query_one("#status").tokens == 42
        assert feed.get_item(0).is_expanded is False


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


def test_event_detail_box_treats_tool_arguments_as_plain_text():
    panel = EventDetailBox()
    event = TuiEvent(
        timestamp=1,
        event_type="tool.completed",
        data={
            "type": "tool.completed",
            "tool_id": "shell_execute",
            "input": {
                "command": '[/Users/huanggui/workspace/yakDB/.venv/bin/python,", "smoke_test.py]',
            },
            "output": {"exit_code": 0, "stdout": "ok"},
        },
    )

    panel._make_renderable(_format_event_detail(event))
    panel.set_event(event)


def test_event_detail_box_treats_tool_output_as_plain_text():
    panel = EventDetailBox()
    event = TuiEvent(
        timestamp=1,
        event_type="tool.completed",
        data={
            "type": "tool.completed",
            "tool_id": "shell_execute",
            "input": {"command": "python smoke_test.py"},
            "output": {"stdout": '[/Users/huanggui/workspace/yakDB/.venv/bin/python,", "smoke_test.py]'},
        },
    )

    panel._make_renderable(_format_event_detail(event))
    panel.set_event(event)


def test_event_detail_box_treats_llm_request_messages_as_plain_text():
    panel = EventDetailBox()
    event = TuiEvent(
        timestamp=1,
        event_type="llm.requested",
        data={
            "type": "llm.requested",
            "model": "qwen3.7-max",
            "messages": [
                {"role": "system", "content": "inspect the trace"},
                {
                    "role": "assistant",
                    "content": '[/Users/huanggui/workspace/yakDB/.venv/bin/python,"\n  "smoke_test.py]',
                },
            ],
        },
    )

    panel._make_renderable(_format_event_detail(event))
    panel.set_event(event)


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
    assert detail.startswith("thinking through evidence")
    assert "thinking:" not in detail
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
    assert detail.startswith("final answer")
    assert "LLM Response" not in detail
    assert "content:" not in detail
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
        assert str(_format_event_line(stream_event)) == "Thought for 0s 2 tokens >"
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
