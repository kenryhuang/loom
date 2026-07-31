from __future__ import annotations

import pytest

from loom.tui.tui_collector import TuiEventCollector


@pytest.mark.asyncio
async def test_tui_collector_tracks_llm_stream_duration() -> None:
    collector = TuiEventCollector()

    started = await collector.emit({"type": "llm.stream.started", "llm_call_id": "llm-1"})
    completed = await collector.emit({"type": "llm.stream.completed", "llm_call_id": "llm-1"})

    assert started.ok
    assert completed.ok
    assert collector.events[-1].event_type == "llm.stream.completed"
    assert collector.events[-1].duration_ms is not None


@pytest.mark.asyncio
async def test_tui_collector_preserves_every_plan_event_in_history_and_queue() -> None:
    collector = TuiEventCollector()

    for revision, event_type in enumerate(
        ("plan.entered", "plan.submitted", "plan.updated")
    ):
        result = await collector.emit(
            {
                "type": event_type,
                "plan_id": "plan_test",
                "revision": revision,
                "plan": {"plan_id": "plan_test", "items": []},
            }
        )
        assert result.ok

    queued = [await collector.queue.get() for _ in range(3)]

    assert collector.event_count == 3
    assert [event.event_type for event in collector.events] == [
        "plan.entered",
        "plan.submitted",
        "plan.updated",
    ]
    assert [event.event_type for event in queued] == [
        "plan.entered",
        "plan.submitted",
        "plan.updated",
    ]
