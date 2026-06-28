import json

from loom.evolution.episodes import StepEpisode, TraceRecord, build_step_episodes, load_trace_records


def _event(event_type, trace_id="trace-1", run_id="run-1", payload=None):
    payload = {
        "type": event_type,
        "run_id": run_id,
        "loop_id": "loop-1",
        "trace_id": trace_id,
        "step_number": 0,
        **(payload or {}),
    }
    return {
        "type": "event",
        "eventType": event_type,
        "traceId": trace_id,
        "payload": payload,
        "hash": f"hash-{event_type}",
    }


def _trace(trace_id="trace-1", run_id="run-1"):
    return {
        "type": "trace",
        "id": trace_id,
        "runId": run_id,
        "payload": {
            "id": trace_id,
            "run_id": run_id,
            "loop_id": "loop-1",
            "loop_version": "v1",
            "step_number": 0,
            "root_trace_id": trace_id,
            "started_at": "2026-06-28T00:00:00Z",
            "ended_at": "2026-06-28T00:00:01Z",
            "duration_ms": 1,
            "input_context_id": "ctx-in",
            "output_context_id": "ctx-out",
            "outcome": "pass",
            "metadata": {"tokenUsage": {"totalTokens": 42}},
        },
        "hash": "hash-trace",
    }


def test_load_trace_records_reads_jsonl_in_order(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record, sort_keys=True)
            for record in (_event("run.started", trace_id=None), _event("step.started"), _trace())
        )
        + "\n",
        encoding="utf-8",
    )

    records = load_trace_records(path)

    assert [record.record_type for record in records] == ["event", "event", "trace"]
    assert records[1].event_type == "step.started"
    assert records[2].trace_id == "trace-1"


def test_build_step_episodes_groups_llm_tool_and_completed_trace():
    records = (
        _event("run.started", trace_id=None),
        _event("step.started"),
        _event("llm.requested", payload={"messages": [{"role": "user", "content": "inspect"}]}),
        _event("llm.completed", payload={"response": {"content": "use tool"}}),
        _event("action.proposed", payload={"action": {"kind": "tool", "target": "inspect-project"}}),
        _event("tool.started", payload={"tool_id": "inspect-project", "input": {}}),
        _event("tool.completed", payload={"tool_id": "inspect-project", "output": {"ok": True}}),
        _event("observation.recorded", payload={"observation": {"source": "inspect-project"}}),
        _event("step.completed"),
        _trace(),
        _event("run.completed", trace_id=None),
    )

    episodes = build_step_episodes(records)

    assert len(episodes) == 1
    episode = episodes[0]
    assert isinstance(episode, StepEpisode)
    assert episode.run_id == "run-1"
    assert episode.trace_id == "trace-1"
    assert episode.loop_id == "loop-1"
    assert episode.step_number == 0
    assert episode.complete is True
    assert [event["type"] for event in episode.llm_requests] == ["llm.requested"]
    assert [event["type"] for event in episode.llm_completions] == ["llm.completed"]
    assert [event["type"] for event in episode.tool_events] == ["tool.started", "tool.completed"]
    assert [event["type"] for event in episode.action_events] == ["action.proposed"]
    assert [event["type"] for event in episode.observation_events] == ["observation.recorded"]
    assert episode.event_hashes == (
        "hash-step.started",
        "hash-llm.requested",
        "hash-llm.completed",
        "hash-action.proposed",
        "hash-tool.started",
        "hash-tool.completed",
        "hash-observation.recorded",
        "hash-step.completed",
        "hash-trace",
    )
    assert episode.completed_trace["id"] == "trace-1"


def test_build_step_episodes_marks_missing_completion_incomplete():
    episodes = build_step_episodes((_event("step.started"), _event("llm.requested")))

    assert len(episodes) == 1
    assert episodes[0].complete is False
    assert episodes[0].completed_event is None
    assert episodes[0].completed_trace is None


def test_episode_and_record_constructors_match_planned_contract():
    record = TraceRecord(
        record_type="event",
        payload={"type": "step.started"},
        event_type="step.started",
        trace_id="trace-1",
        run_id="run-1",
        hash="hash-step.started",
    )
    episode = StepEpisode(
        run_id="run-1",
        trace_id="trace-1",
        loop_id="loop-1",
        step_number=0,
        started_event={"type": "step.started"},
        llm_requests=(),
        llm_completions=(),
        tool_events=(),
        action_events=(),
        observation_events=(),
        completed_trace=None,
        completed_event=None,
        event_hashes=("hash-step.started",),
    )

    assert record.raw is None
    assert episode.complete is False
