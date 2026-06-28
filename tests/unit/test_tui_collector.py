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
