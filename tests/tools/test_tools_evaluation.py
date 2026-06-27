from loom.core import Action, Trace, new_run_id
from loom.tools import PromotionEvidence, detect_tool_patterns, should_promote


def _trace(trace_id, context_id, tool_ids, *, outcome="pass", confidence=0.8):
    actions = tuple(Action(f"{trace_id}-{tool_id}", "tool", f"Use {tool_id}", target=tool_id) for tool_id in tool_ids)
    return Trace(
        id=trace_id,
        run_id=new_run_id(),
        loop_id="loop",
        loop_version="v1",
        step_number=0,
        root_trace_id=trace_id,
        started_at="2026-06-27T00:00:00.000Z",
        ended_at="2026-06-27T00:00:00.001Z",
        duration_ms=1,
        input_context_id=context_id,
        output_context_id=f"{context_id}-out",
        outcome=outcome,
        actions=actions,
        metadata={"decisionConfidence": confidence},
    )


def test_detect_tool_patterns_counts_distinct_contexts():
    traces = (
        _trace("trace-1", "ctx-a", ("search", "read")),
        _trace("trace-2", "ctx-b", ("read", "search")),
        _trace("trace-3", "ctx-c", ("search", "write")),
    )

    patterns = detect_tool_patterns(traces, min_distinct_contexts=2)

    assert len(patterns) == 1
    assert patterns[0].tool_ids == ("read", "search")
    assert patterns[0].trace_ids == ("trace-1", "trace-2")
    assert patterns[0].distinct_context_shapes == 2


def test_should_promote_requires_at_least_two_evidence_categories():
    weak = PromotionEvidence(reuse=False, compression=True, quality=False, stability=False, auditability=False)
    strong = PromotionEvidence(reuse=True, compression=True, quality=False, stability=False, auditability=False)

    assert should_promote(weak) is False
    assert should_promote(strong) is True
