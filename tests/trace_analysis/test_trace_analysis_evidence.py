from loom.trace_analysis.evidence import evidence_ref_for_event, value_at_path
from loom.trace_analysis.schemas import NormalizedEvent


def _event():
    return NormalizedEvent(
        record_id="hash-1",
        event_type="tool.completed",
        run_id="run-1",
        loop_id="loop-1",
        trace_id="trace-1",
        step_number=0,
        llm_call_id=None,
        tool_call_id="tool-1",
        tool_id="shell_execute",
        at=None,
        payload={"type": "tool.completed", "output": {"stderr": "alpha\nbeta\ngamma"}},
        hash="hash-1",
    )


def test_value_at_path_reads_nested_dicts():
    assert value_at_path(_event().payload, "output.stderr") == "alpha\nbeta\ngamma"


def test_evidence_ref_for_event_keeps_compact_excerpt():
    ref = evidence_ref_for_event(_event(), field_path="output.stderr", max_excerpt_chars=10)

    assert ref.event_hash == "hash-1"
    assert ref.event_type == "tool.completed"
    assert ref.subject_id == "tool:run-1:trace-1:0:tool-1"
    assert ref.field_path == "output.stderr"
    assert ref.excerpt == "alpha..."
