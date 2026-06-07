import asyncio

from loom.core import (
    Trace,
    as_step_number,
    make_loom_error,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    ok,
)
from loom.observability import (
    InMemoryTraceStore,
    create_in_memory_trace_sink,
)

NOW = "2026-06-04T00:00:00.000Z"


def test_store_appends_gets_queries_and_tracks_events():
    async def scenario():
        store = InMemoryTraceStore()
        run_id = new_run_id()
        loop_id = new_loop_id()
        parent = make_trace(run_id=run_id, loop_id=loop_id, tags=("root", "shared"))
        child = make_trace(
            run_id=run_id,
            loop_id=loop_id,
            parent_trace_id=parent.id,
            root_trace_id=parent.id,
            outcome="fail",
            tags=("child", "shared"),
        )

        assert await store.append(parent) == ok(None)
        assert await store.append(child) == ok(None)
        assert await store.get(parent.id) == ok(parent)

        assert [trace.id async for trace in store.query({"run_id": run_id})] == [
            parent.id,
            child.id,
        ]
        assert [trace.id async for trace in store.query({"parent_trace_id": parent.id})] == [child.id]
        assert [trace.id async for trace in store.query({"outcome": ("fail",)})] == [child.id]
        assert [trace.id async for trace in store.query({"tags": ("shared",)})] == [
            parent.id,
            child.id,
        ]
        assert [trace.id async for trace in store.children(parent.id)] == [child.id]

        started = {
            "type": "step.started",
            "trace_id": parent.id,
            "at": NOW,
            "context_id": parent.input_context_id,
        }
        completed_trace = make_trace(run_id=new_run_id(), loop_id=new_loop_id())
        completed = {"type": "step.completed", "trace": completed_trace, "at": NOW}
        await store.append_event(started)
        await store.append_event(completed)

        assert store.events() == (started, completed)
        assert await store.get(completed_trace.id) == ok(completed_trace)

    asyncio.run(scenario())


def test_store_reports_missing_and_sink_persists_completed_traces():
    async def scenario():
        store = InMemoryTraceStore()
        missing_id = new_trace_id()
        missing = await store.get(missing_id)
        assert missing.ok is False
        assert missing.error.code == "VALIDATION_FAILED"
        assert missing.error.trace_id == missing_id

        sink = create_in_memory_trace_sink(store)
        trace = make_trace(run_id=new_run_id(), loop_id=new_loop_id())
        assert await sink.emit({"type": "step.completed", "trace": trace, "at": NOW}) == ok(None)
        assert await store.get(trace.id) == ok(trace)

    asyncio.run(scenario())


def make_trace(
    *,
    run_id,
    loop_id,
    parent_trace_id=None,
    root_trace_id=None,
    outcome="pass",
    tags=(),
):
    trace_id = new_trace_id()
    return Trace(
        id=trace_id,
        run_id=run_id,
        loop_id=loop_id,
        loop_version=new_loop_version(),
        step_number=as_step_number(0),
        parent_trace_id=parent_trace_id,
        root_trace_id=root_trace_id or trace_id,
        started_at=NOW,
        ended_at=NOW,
        duration_ms=1,
        input_context_id="ctx-in",
        output_context_id="ctx-out",
        outcome=outcome,
        error=(make_loom_error("LOOP_FAILED", "failed", retryable=False) if outcome == "fail" else None),
        tags=tags,
    )
