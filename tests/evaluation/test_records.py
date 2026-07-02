import json

from loom.evaluation.records import NormalizedEvent, load_normalized_events


def test_load_normalized_events_reads_event_and_trace_records(tmp_path):
    path = tmp_path / "trace.jsonl"
    records = [
        {
            "type": "event",
            "eventType": "step.started",
            "traceId": "trace-1",
            "payload": {
                "type": "step.started",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
                "at": "2026-07-03T00:00:00Z",
            },
            "hash": "hash-step-started",
        },
        {
            "type": "event",
            "eventType": "tool.completed",
            "traceId": "trace-1",
            "payload": {
                "type": "tool.completed",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
                "tool_id": "read_file",
                "tool_call_id": "call-1",
                "input": {"path": "README.md"},
                "output": {"content": "# Demo"},
            },
            "hash": "hash-tool-completed",
        },
        {
            "type": "trace",
            "id": "trace-1",
            "runId": "run-1",
            "payload": {
                "id": "trace-1",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "step_number": 0,
                "outcome": "pass",
            },
            "hash": "hash-trace",
        },
    ]
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")

    result = load_normalized_events(path)

    assert result.ok
    events = result.value.events
    assert all(isinstance(event, NormalizedEvent) for event in events)
    assert tuple(event.event_type for event in events) == ("step.started", "tool.completed", "trace.completed")
    assert events[0].run_id == "run-1"
    assert events[0].loop_id == "loop-1"
    assert events[0].trace_id == "trace-1"
    assert events[0].step_number == 0
    assert events[1].tool_id == "read_file"
    assert events[1].tool_call_id == "call-1"
    assert events[2].hash == "hash-trace"
    assert result.value.corrupt_records == ()


def test_load_normalized_events_returns_validation_error_for_malformed_json(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text("{not-json\n", encoding="utf-8")

    result = load_normalized_events(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert result.error.metadata["path"] == str(path)
