from loom.trace_analysis.graph import build_episode_graph
from loom.trace_analysis.schemas import NormalizedEvent


def _event(
    event_type,
    *,
    hash,
    run_id="run-1",
    loop_id="loop-1",
    trace_id="trace-1",
    step_number=0,
    llm_call_id=None,
    tool_call_id=None,
    tool_id=None,
):
    return NormalizedEvent(
        record_id=hash,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=llm_call_id,
        tool_call_id=tool_call_id,
        tool_id=tool_id,
        at=None,
        payload={"type": event_type, "run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step_number},
        hash=hash,
    )


def test_episode_graph_exposes_runtime_steps_and_steps_alias():
    graph = build_episode_graph(
        (
            _event("run.started", hash="run-start", trace_id=None, step_number=None),
            _event("step.started", hash="step-start"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
            _event("run.completed", hash="run-complete", trace_id=None, step_number=None),
        )
    )

    assert len(graph.runs) == 1
    assert len(graph.runtime_steps) == 1
    assert graph.steps == graph.runtime_steps
    assert graph.runtime_steps[0].status == "complete"


def test_episode_graph_groups_multiple_llm_rounds_inside_one_runtime_step():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-1-request"),
            _event("llm.completed", llm_call_id="llm-1", hash="llm-1-complete"),
            _event("tool.started", tool_call_id="tool-1", tool_id="read_file", hash="tool-1-start"),
            _event("tool.completed", tool_call_id="tool-1", tool_id="read_file", hash="tool-1-complete"),
            _event("llm.requested", llm_call_id="llm-2", hash="llm-2-request"),
            _event("llm.completed", llm_call_id="llm-2", hash="llm-2-complete"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
        )
    )

    assert len(graph.runtime_steps) == 1
    assert len(graph.llm_rounds) == 2
    assert len(graph.tool_calls) == 1
    assert graph.llm_rounds[0].requested_event is not None
    assert graph.llm_rounds[0].completed_event is not None
    assert graph.tool_calls[0].started_event is not None
    assert graph.tool_calls[0].completed_event is not None
