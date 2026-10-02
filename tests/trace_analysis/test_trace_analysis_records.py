import json

from loom.trace_analysis.records import load_normalized_events, normalize_record
from loom.trace_analysis.schemas import NormalizedEvent


def test_normalize_record_reads_wrapper_and_payload_identity():
    event = normalize_record(
        {
            "type": "event",
            "eventType": "llm.completed",
            "traceId": "trace-1",
            "hash": "hash-1",
            "payload": {
                "type": "llm.completed",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": "2",
                "llm_call_id": "llm-1",
                "response": {"usage": {"total_tokens": 10}},
            },
        },
        line_number=7,
    )

    assert isinstance(event, NormalizedEvent)
    assert event.record_id == "hash-1"
    assert event.event_type == "llm.completed"
    assert event.run_id == "run-1"
    assert event.loop_id == "loop-1"
    assert event.trace_id == "trace-1"
    assert event.step_number == 2
    assert event.llm_call_id == "llm-1"
    assert event.hash == "hash-1"
    assert event.line_number == 7


def test_load_normalized_events_reads_jsonl(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "event",
                "eventType": "run.started",
                "payload": {"type": "run.started", "run_id": "run-1", "loop_id": "loop-1"},
                "hash": "run-start",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = load_normalized_events(path)

    assert result.ok
    assert len(result.value.events) == 1
    assert result.value.events[0].event_type == "run.started"


def test_normalize_record_reads_call_ids_from_metadata():
    event = normalize_record(
        {
            "type": "event",
            "eventType": "tool.completed",
            "payload": {
                "type": "tool.completed",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
                "metadata": {
                    "llm_call_id": "llm-1",
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                },
            },
        }
    )

    assert event.llm_call_id == "llm-1"
    assert event.tool_call_id == "call-1"
    assert event.tool_id == "read_file"
