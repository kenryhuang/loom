import json
from pathlib import Path

import pytest

from loom.evaluation.base_metrics import build_base_statistics
from loom.evaluation.evidence_store import EvidenceStore
from loom.evaluation.metrics import calculate_metrics
from loom.evaluation.token_ledger import build_token_ledger
from loom.trace_analysis import build_episode_graph


def event(kind, second, **payload):
    return {"type": kind, "run_id": "r", "loop_id": "l", "trace_id": "t", "step_number": 0,
            "at": f"2026-10-10T00:00:{second:02d}Z", **payload}


def analyze(rows, **kwargs):
    store = EvidenceStore(Path("fixture.jsonl"), "\n".join(json.dumps(r) for r in rows).encode())
    graph = build_episode_graph(store.events)
    ledger, _ = build_token_ledger(store, graph)
    return build_base_statistics(store, graph, ledger, **kwargs), graph


def test_pause_resume_and_concurrent_calls_partition_time_without_double_counting():
    rows = [event("run.started", 0), event("step.started", 0),
            event("llm.requested", 1, llm_call_id="m1", model="one"),
            event("llm.completed", 5, llm_call_id="m1", response={"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}),
            event("tool.started", 5, tool_id="shell_execute", tool_call_id="a"),
            event("tool.started", 6, tool_id="read_file", tool_call_id="b"),
            event("llm.requested", 7, llm_call_id="verify", model="two", usage_role="verification", acceptance_stage="verify"),
            event("llm.completed", 8, llm_call_id="verify", response={"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}),
            event("tool.completed", 9, tool_id="read_file", tool_call_id="b", output={"content": "read", "truncated": True}),
            event("tool.completed", 10, tool_id="shell_execute", tool_call_id="a", output={"exit_code": 0}),
            event("run.state.changed", 10, state="suspended"),
            event("run.started", 20, loop_id="resumed"), event("step.started", 20, loop_id="resumed"),
            event("llm.requested", 20, loop_id="resumed", llm_call_id="unfinished"),
            event("tool.started", 20, loop_id="resumed", tool_id="read_file", tool_call_id="cancel"),
            event("tool.cancelled", 21, loop_id="resumed", tool_id="read_file", tool_call_id="cancel"),
            event("step.completed", 25, loop_id="resumed"), event("run.state.changed", 25, loop_id="resumed", state="completed")]
    stats, _ = analyze(rows)
    assert stats["basic"]["steps"] == 1 and stats["basic"]["step_attempts"] == 2
    assert stats["timing"]["wall_ms"] == 25000
    assert stats["timing"]["states_ms"]["running"] == 15000
    assert stats["timing"]["states_ms"]["paused"] == 10000
    assert stats["timing"]["running_activity_ms"] == {"model_only": 4000, "tool_only": 5000, "overlap": 1000, "unattributed": 5000}
    assert stats["tools"]["duration"]["total_ms"] == 9000
    assert stats["tools"]["success"] == 2 and stats["tools"]["cancelled"] == 1
    assert stats["tools"]["success_rate"] == 1
    assert stats["models"]["total_tokens"]["known"] == 18
    assert stats["models"]["incomplete"] == 1 and stats["quality"]["missing_usage_calls"] == 1
    assert next(r for r in stats["models"]["by_role"] if r["name"] == "verification")["total_tokens"]["known"] == 3
    assert stats["tools"]["truncated_outputs"] == 1


@pytest.mark.parametrize(("output", "status", "reason"), [
    ({"exit_code": 2}, "failed", "execution"), ({"exit_code": 1, "status": "no_match"}, "success", None),
    ({"timed_out": True}, "failed", "timeout"), ({"cancelled": True}, "cancelled", "cancelled"),
    ({"accepted": False}, "failed", "rejected"), ({"ok": False, "error": {"code": "VALIDATION_FAILED"}}, "failed", "invalid_input"),
    ({"ok": False, "error": {"code": "EXECUTION_UNKNOWN"}}, "unknown", "uncertain_effect"),
])
def test_tool_status_and_legacy_success_rate_share_one_definition(output, status, reason):
    stats, graph = analyze([event("tool.started", 1, tool_id="shell_execute", tool_call_id="a"),
                            event("tool.completed", 3, tool_id="shell_execute", tool_call_id="a", output=output)])
    assert stats["calls"]["tools"][0]["status"] == status
    assert stats["calls"]["tools"][0]["failure_reason"] == reason
    metrics = {m.name: m.value for m in calculate_metrics(graph)}
    assert metrics["tool.success_rate"] == stats["tools"]["success_rate"]
    assert metrics["tool.failure_count"] == stats["tools"]["failed"]


def test_missing_invalid_timestamps_and_duplicate_usage_are_explicit():
    end = event("llm.completed", 1, llm_call_id="m", response={"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}})
    stats, graph = analyze([event("llm.requested", 2, llm_call_id="m"), end, end,
                            {**event("tool.started", 3, tool_id="read_file", tool_call_id="t"), "at": None}])
    assert stats["models"]["total_tokens"]["known"] == 15
    assert stats["quality"]["usage_statuses"] == {"duplicate": 1}
    assert stats["quality"]["duplicate_records"] == 1
    assert stats["quality"]["missing_call_durations"] == 2
    assert stats["timing"]["clock_reversals"] == 1
    assert stats["tools"]["success_rate"] is None
    assert next(m.value for m in calculate_metrics(graph) if m.name == "cost.total_tokens") == 15


def test_source_and_run_scopes_keep_raw_counts_and_usage_separate():
    rows = [event("run.started", 0), event("llm.requested", 1, llm_call_id="m"),
            event("llm.completed", 2, llm_call_id="m", response={"usage": {"total_tokens": 5}}), event("run.completed", 3),
            event("run.started", 4, run_id="other"), event("run.completed", 6, run_id="other")]
    counts = {"r": {"llm.content.delta": 90, "llm.requested": 1, "llm.completed": 1, "run.started": 1, "run.completed": 1},
              "other": {"run.started": 1, "run.completed": 1}}
    stats, _ = analyze(rows, coverage={"raw_event_count": 96, "event_counts_by_run": counts})
    assert stats["basic"]["events"]["raw"] == 96 and stats["basic"]["events"]["analyzed"] == 6
    assert stats["by_run"]["r"]["basic"]["events"]["raw"] == 94
    assert stats["by_run"]["other"]["models"]["calls"] == 0
    assert stats["timing"]["wall_ms"] == 5000 and stats["timing"]["session_span_ms"] == 6000
    assert stats["by_run"]["r"]["timing"]["wall_ms"] == 3000


def test_empty_source_has_no_invented_duration_or_success_rate():
    stats, _ = analyze([])
    assert stats["timing"]["wall_ms"] is None
    assert stats["tools"]["success_rate"] is None
    assert stats["models"]["duration"]["average_ms"] is None
