from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.metrics import calculate_metrics
from loom.evaluation.records import NormalizedEvent


def _event(
    event_type,
    *,
    run_id="run-1",
    loop_id="loop-1",
    trace_id="trace-1",
    step_number=0,
    tool_call_id=None,
    tool_id=None,
    payload=None,
    hash=None,
):
    event_payload = {"type": event_type, "run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step_number}
    if payload:
        event_payload.update(payload)
    return NormalizedEvent(
        record_id=hash or event_type,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=None,
        tool_call_id=tool_call_id,
        tool_id=tool_id,
        at=None,
        payload=event_payload,
        hash=hash,
    )


def test_calculate_metrics_reports_trace_and_tool_quality():
    graph = build_episode_graph(
        (
            _event("run.started", trace_id=None, step_number=None, hash="run-start"),
            _event("step.started", hash="step-start"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", payload={"output": {"content": "abc"}}, hash="tool-complete"),
            _event("tool.failed", tool_call_id="call-2", tool_id="shell_execute", hash="tool-failed"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", payload={"outcome": "pass", "metadata": {"tokenUsage": {"totalTokens": 42}}}, hash="trace-complete"),
            _event("run.completed", trace_id=None, step_number=None, hash="run-complete"),
        )
    )

    metrics = calculate_metrics(graph)
    by_name = {metric.name: metric for metric in metrics}

    assert by_name["trace.completeness"].value == 1.0
    assert by_name["tool.call_count"].value == 2
    assert by_name["tool.failure_count"].value == 1
    assert by_name["tool.success_rate"].value == 0.5
    assert by_name["cost.total_tokens"].value == 42
    assert by_name["episode.partial_count"].value == 0
    assert by_name["episode.orphaned_count"].value == 0
