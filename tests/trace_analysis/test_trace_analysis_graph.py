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
    payload=None,
):
    event_payload = {"type": event_type, "run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step_number}
    if payload:
        event_payload.update(payload)
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
        payload=event_payload,
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


def test_episode_graph_keeps_llm_rounds_in_request_insertion_order():
    graph = build_episode_graph(
        tuple(
            event
            for call_id in ("llm-1", "llm-2", "llm-10")
            for event in (
                _event("llm.requested", llm_call_id=call_id, hash=f"{call_id}-request"),
                _event("llm.completed", llm_call_id=call_id, hash=f"{call_id}-complete"),
            )
        )
    )

    assert [round_item.llm_call_id for round_item in graph.llm_rounds] == ["llm-1", "llm-2", "llm-10"]


def test_episode_graph_disambiguates_same_step_and_call_ids_in_different_loops():
    graph = build_episode_graph(
        (
            _event("step.started", loop_id="loop-1", hash="loop-1-step"),
            _event("llm.requested", loop_id="loop-1", llm_call_id="llm-1", hash="loop-1-llm-request"),
            _event("tool.started", loop_id="loop-1", tool_call_id="call-1", tool_id="read_file", hash="loop-1-tool"),
            _event("step.started", loop_id="loop-2", hash="loop-2-step"),
            _event("llm.requested", loop_id="loop-2", llm_call_id="llm-1", hash="loop-2-llm-request"),
            _event("tool.started", loop_id="loop-2", tool_call_id="call-1", tool_id="read_file", hash="loop-2-tool"),
        )
    )

    assert len(graph.steps) == 2
    assert len({step.id for step in graph.steps}) == 2
    assert len(graph.llm_rounds) == 2
    assert len({round_item.id for round_item in graph.llm_rounds}) == 2
    assert len(graph.tool_calls) == 2
    assert len({tool.id for tool in graph.tool_calls}) == 2
    assert all(len(step.llm_round_ids) == 1 for step in graph.steps)
    assert all(len(step.tool_call_ids) == 1 for step in graph.steps)


def test_tool_events_with_metadata_llm_id_do_not_create_llm_rounds():
    graph = build_episode_graph(
        (
            _event("tool.started", llm_call_id="llm-1", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event("tool.failed", llm_call_id="llm-1", tool_call_id="call-1", tool_id="read_file", hash="tool-failed"),
        )
    )

    assert graph.llm_rounds == ()
    assert len(graph.tool_calls) == 1
    assert graph.tool_calls[0].status == "failed"


def test_idless_llm_failure_is_preserved_as_unmatched_event():
    failed = _event("llm.failed", hash="llm-failed")

    graph = build_episode_graph((_event("step.started", hash="step-start"), failed))

    assert graph.llm_rounds == ()
    assert graph.orphaned_events == (failed,)


def test_anonymous_tool_calls_pair_only_with_a_unique_open_invocation():
    graph = build_episode_graph(
        (
            _event("tool.started", tool_id="read_file", hash="first-start"),
            _event("tool.completed", tool_id="read_file", hash="first-complete"),
            _event("tool.started", tool_id="read_file", hash="second-start"),
            _event("tool.completed", tool_id="read_file", hash="second-complete"),
        )
    )

    assert len(graph.tool_calls) == 2
    assert [item.status for item in graph.tool_calls] == ["complete", "complete"]
    assert [item.event_hashes for item in graph.tool_calls] == [
        ("first-start", "first-complete"),
        ("second-start", "second-complete"),
    ]


def test_ambiguous_anonymous_tool_terminal_does_not_force_a_match():
    graph = build_episode_graph(
        (
            _event("tool.started", tool_id="read_file", hash="first-start"),
            _event("tool.started", tool_id="read_file", hash="second-start"),
            _event("tool.completed", tool_id="read_file", hash="ambiguous-complete"),
        )
    )

    assert len(graph.tool_calls) == 3
    assert all(item.status == "partial" for item in graph.tool_calls)
    assert [item.event_hashes for item in graph.tool_calls] == [
        ("first-start",),
        ("second-start",),
        ("ambiguous-complete",),
    ]


def test_same_scope_call_id_reuse_creates_sequential_occurrences():
    graph = build_episode_graph(
        (
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request-1"),
            _event("llm.completed", llm_call_id="llm-1", hash="llm-complete-1"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request-2"),
            _event("llm.failed", llm_call_id="llm-1", hash="llm-failed-2"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start-1"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", hash="tool-complete-1"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start-2"),
            _event("tool.failed", tool_call_id="call-1", tool_id="read_file", hash="tool-failed-2"),
        )
    )

    assert [item.status for item in graph.llm_rounds] == ["complete", "failed"]
    assert [item.event_hashes for item in graph.llm_rounds] == [
        ("llm-request-1", "llm-complete-1"),
        ("llm-request-2", "llm-failed-2"),
    ]
    assert graph.llm_rounds[0].id == "llm:run-1:trace-1:0:llm-1"
    assert graph.llm_rounds[1].id == "llm:run-1:trace-1:0:llm-1:occurrence:2"
    assert [item.status for item in graph.tool_calls] == ["complete", "failed"]
    assert [item.event_hashes for item in graph.tool_calls] == [
        ("tool-start-1", "tool-complete-1"),
        ("tool-start-2", "tool-failed-2"),
    ]
    assert graph.tool_calls[0].id == "tool:run-1:trace-1:0:call-1"
    assert graph.tool_calls[1].id == "tool:run-1:trace-1:0:call-1:occurrence:2"


def test_overlapping_explicit_call_id_reuse_keeps_terminal_unmatched():
    llm_complete = _event("llm.completed", llm_call_id="llm-1", hash="ambiguous-llm-complete")
    tool_complete = _event(
        "tool.completed",
        tool_call_id="call-1",
        tool_id="read_file",
        hash="ambiguous-tool-complete",
    )
    graph = build_episode_graph(
        (
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request-1"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request-2"),
            llm_complete,
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start-1"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start-2"),
            tool_complete,
        )
    )

    assert len(graph.llm_rounds) == 2
    assert all(item.status == "partial" for item in graph.llm_rounds)
    assert len(graph.tool_calls) == 2
    assert all(item.status == "partial" for item in graph.tool_calls)
    assert graph.orphaned_events == (llm_complete, tool_complete)


def test_repeated_terminal_without_new_start_is_duplicate_not_new_occurrence():
    graph = build_episode_graph(
        (
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request"),
            _event("llm.completed", llm_call_id="llm-1", hash="llm-complete"),
            _event("llm.completed", llm_call_id="llm-1", hash="llm-complete-duplicate"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", hash="tool-complete"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", hash="tool-complete-duplicate"),
        )
    )

    assert len(graph.llm_rounds) == 1
    assert graph.llm_rounds[0].event_hashes == ("llm-request", "llm-complete", "llm-complete-duplicate")
    assert len(graph.tool_calls) == 1
    assert graph.tool_calls[0].event_hashes == ("tool-start", "tool-complete", "tool-complete-duplicate")
    assert graph.orphaned_events == ()


def test_conflicting_terminal_without_new_start_is_unmatched_not_new_occurrence():
    conflicting_llm = _event(
        "llm.completed",
        llm_call_id="llm-1",
        payload={"response": {"usage": {"total_tokens": 20}}},
        hash="llm-conflict",
    )
    conflicting_tool = _event(
        "tool.completed",
        tool_call_id="call-1",
        tool_id="read_file",
        payload={"output": {"content": "changed"}},
        hash="tool-conflict",
    )
    graph = build_episode_graph(
        (
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request"),
            _event(
                "llm.completed",
                llm_call_id="llm-1",
                payload={"response": {"usage": {"total_tokens": 10}}},
                hash="llm-complete",
            ),
            conflicting_llm,
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event(
                "tool.completed",
                tool_call_id="call-1",
                tool_id="read_file",
                payload={"output": {"content": "original"}},
                hash="tool-complete",
            ),
            conflicting_tool,
        )
    )

    assert len(graph.llm_rounds) == 1
    assert graph.llm_rounds[0].event_hashes == ("llm-request", "llm-complete")
    assert len(graph.tool_calls) == 1
    assert graph.tool_calls[0].event_hashes == ("tool-start", "tool-complete")
    assert graph.orphaned_events == (conflicting_llm, conflicting_tool)
