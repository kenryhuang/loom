from loom.evaluation.assessments import assess_steps
from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.records import NormalizedEvent


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


def test_assess_steps_flags_missing_llm_completion_and_tool_failure():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request"),
            _event("tool.started", tool_call_id="call-1", tool_id="shell_execute", hash="tool-start"),
            _event("tool.failed", tool_call_id="call-1", tool_id="shell_execute", hash="tool-failed"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
        )
    )

    assessment = assess_steps(graph)[0]

    assert assessment.status == "fail"
    assert assessment.aggregate_score < 1.0
    assert assessment.dimensions["tool_execution"].score < 1.0
    assert assessment.dimensions["llm_response"].score < 1.0
    assert {finding.category for finding in assessment.findings} >= {"tool_failure", "llm_round_incomplete"}
    assert any("tool-failed" in finding.evidence_event_hashes for finding in assessment.findings)


def test_assess_steps_marks_complete_clean_step_as_pass():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request"),
            _event("llm.completed", llm_call_id="llm-1", payload={"response": {"usage": {"total_tokens": 32}}}, hash="llm-complete"),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", hash="tool-start"),
            _event("tool.completed", tool_call_id="call-1", tool_id="read_file", hash="tool-complete"),
            _event("llm.requested", llm_call_id="llm-2", hash="llm-request-2"),
            _event("llm.completed", llm_call_id="llm-2", hash="llm-complete-2"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", payload={"outcome": "pass"}, hash="trace-complete"),
        )
    )

    assessment = assess_steps(graph)[0]

    assert assessment.status == "pass"
    assert assessment.aggregate_score == 1.0
    assert not assessment.findings
    assert all(dimension.score == 1.0 for dimension in assessment.dimensions.values())


def test_assess_steps_does_not_double_count_trace_aggregate_token_usage():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("llm.requested", llm_call_id="llm-1", hash="llm-request"),
            _event("llm.completed", llm_call_id="llm-1", payload={"response": {"usage": {"total_tokens": 20000}}}, hash="llm-complete"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", payload={"metadata": {"tokenUsage": {"totalTokens": 20000}}, "outcome": "pass"}, hash="trace-complete"),
        )
    )

    assessment = assess_steps(graph)[0]

    assert "high_token_usage" not in {finding.category for finding in assessment.findings}
    assert assessment.dimensions["efficiency"].score == 1.0


def test_assess_steps_does_not_mix_event_order_between_loops():
    graph = build_episode_graph(
        (
            _event("step.started", loop_id="loop-1", hash="loop-1-step"),
            _event("llm.requested", loop_id="loop-1", llm_call_id="llm-1", hash="loop-1-llm-request"),
            _event("llm.completed", loop_id="loop-1", llm_call_id="llm-1", hash="loop-1-llm-complete"),
            _event("tool.started", loop_id="loop-1", tool_call_id="call-1", tool_id="read_file", hash="loop-1-tool-start"),
            _event("tool.completed", loop_id="loop-1", tool_call_id="call-1", tool_id="read_file", hash="loop-1-tool-complete"),
            _event("step.started", loop_id="loop-2", hash="loop-2-step"),
            _event("tool.started", loop_id="loop-2", tool_call_id="call-1", tool_id="read_file", hash="loop-2-tool-start"),
            _event("tool.completed", loop_id="loop-2", tool_call_id="call-1", tool_id="read_file", hash="loop-2-tool-complete"),
            _event("llm.requested", loop_id="loop-2", llm_call_id="llm-1", hash="loop-2-llm-request"),
            _event("llm.completed", loop_id="loop-2", llm_call_id="llm-1", hash="loop-2-llm-complete"),
        )
    )

    assessments = {item.loop_id: item for item in assess_steps(graph)}

    assert "tool_result_not_followed_by_llm" in {finding.category for finding in assessments["loop-1"].findings}
    assert "tool_result_not_followed_by_llm" not in {finding.category for finding in assessments["loop-2"].findings}


def test_assess_steps_fails_on_unmatched_idless_llm_failure():
    graph = build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event("llm.failed", hash="llm-failed"),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", hash="trace-complete"),
        )
    )

    assessment = assess_steps(graph)[0]

    assert assessment.status == "fail"
    assert assessment.dimensions["llm_response"].status == "fail"
    assert "llm-failed" in assessment.dimensions["llm_response"].evidence_event_hashes


def test_assessment_and_finding_ids_follow_disambiguated_step_identity():
    graph = build_episode_graph(
        tuple(
            event
            for loop_id in ("loop-1", "loop-2")
            for event in (
                _event("step.started", loop_id=loop_id, hash=f"{loop_id}-start"),
                _event("llm.failed", loop_id=loop_id, hash=f"{loop_id}-failed"),
            )
        )
    )

    assessments = assess_steps(graph)

    assert len({item.id for item in assessments}) == 2
    assert len({finding.id for item in assessments for finding in item.findings}) == sum(
        len(item.findings) for item in assessments
    )


def test_assessment_ids_remain_legacy_shape_without_collision():
    graph = build_episode_graph((_event("step.started", hash="step-start"),))

    assessment = assess_steps(graph)[0]

    assert assessment.id == "assessment:run-1:trace-1:0"
    assert assessment.findings[0].id == "finding:run-1:trace-1:0:step_incomplete"
