from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.records import NormalizedEvent


def _event(
    event_type,
    *,
    run_id="run-1",
    loop_id="loop-1",
    trace_id="trace-1",
    step_number=0,
    llm_call_id=None,
    tool_call_id=None,
    tool_id=None,
    hash=None,
):
    return NormalizedEvent(
        record_id=hash or event_type,
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


def test_build_episode_graph_groups_run_step_llm_and_tool_episodes():
    graph = build_episode_graph(
        (
            _event("run.started", trace_id=None, step_number=None, hash="run-start"),
            _event("step.started", hash="step-start"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request"),
            _event("llm.completed", llm_call_id="llm-1", hash="llm-complete"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", hash="tool-complete"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
            _event("run.completed", trace_id=None, step_number=None, hash="run-complete"),
        )
    )

    assert len(graph.runs) == 1
    assert graph.runs[0].status == "complete"
    assert len(graph.steps) == 1
    assert graph.steps[0].status == "complete"
    assert graph.steps[0].run_id == "run-1"
    assert graph.steps[0].trace_id == "trace-1"
    assert len(graph.llm_rounds) == 1
    assert graph.llm_rounds[0].status == "complete"
    assert graph.llm_rounds[0].llm_call_id == "llm-1"
    assert len(graph.tool_calls) == 1
    assert graph.tool_calls[0].status == "complete"
    assert graph.tool_calls[0].tool_id == "read_file"
    assert "tool-complete" in graph.event_hashes


def test_build_episode_graph_marks_partial_tool_call():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("tool.started", tool_call_id="call-1", tool_id="shell_execute", hash="tool-start"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
        )
    )

    assert graph.steps[0].status == "complete"
    assert graph.tool_calls[0].status == "partial"
