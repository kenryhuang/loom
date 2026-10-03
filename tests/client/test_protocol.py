import pytest

from loom.client.projection import SessionProjection
from loom.service.contracts import ServiceError


def event(seq, kind, payload, sid="s"):
    return {"session_id": sid, "seq": seq, "type": kind, "payload": payload}


def test_projection_deduplicates_and_rejects_gaps_and_other_sessions():
    projection = SessionProjection({"session_id": "s", "event_cursor": 2, "messages": [], "task": {"state": "running"}})
    assert projection.apply(event(3, "message.created", {"id": "m", "command_id": "c", "state": "accepted"}))
    assert not projection.apply(event(3, "message.created", {"id": "m"}))
    assert not projection.apply(event(4, "message.created", {}, sid="old"))
    projection.apply(event(4, "command.applied", {"command_id": "c"}))
    assert len(projection.snapshot["messages"]) == 1
    assert projection.snapshot["messages"][0]["state"] == "applied"
    with pytest.raises(ServiceError):
        projection.apply(event(6, "task.state.changed", {"state": "idle"}))


def test_stream_offsets_merge_replays_and_final_response():
    projection = SessionProjection({"session_id": "s", "event_cursor": 0, "messages": [], "task": {}})
    projection.apply(event(1, "llm.content.delta", {"llm_call_id": "l", "delta": "abc", "offset": 0}))
    projection.apply(event(2, "llm.content.delta", {"llm_call_id": "l", "delta": "cde", "offset": 2}))
    assert projection.streams["l:content"] == "abcde"
    projection.apply(event(3, "llm.completed", {"llm_call_id": "l", "response": {"content": "abcdef"}}))
    assert projection.streams["l:content"] == "abcdef"


def test_budget_changes_and_usage_are_projected_without_resetting_run():
    projection = SessionProjection(
        {
            "session_id": "s",
            "event_cursor": 0,
            "messages": [],
            "task": {"limits": {"max_tokens": 100000}},
            "token_budget": {"limit": 100000, "used": 120000, "remaining": 0},
        }
    )
    projection.apply(event(1, "task.budget.changed", {"max_tokens": 10000000}))
    assert projection.snapshot["task"]["limits"]["max_tokens"] == 10000000
    assert projection.snapshot["token_budget"] == {"limit": 10000000, "used": 120000, "remaining": 9880000}
    projection.apply(event(2, "run.usage.changed", {"total_tokens": 130000}))
    assert projection.snapshot["token_budget"]["used"] == 130000


def test_resume_keeps_usage_but_a_new_run_resets_it():
    projection = SessionProjection(
        {
            "session_id": "s",
            "event_cursor": 0,
            "messages": [],
            "task": {"limits": {"max_tokens": 1000}},
            "run": {"id": "first", "state": "suspended", "active_seconds": 30},
            "token_budget": {"limit": 1000, "used": 100, "remaining": 900},
        }
    )
    projection.apply({**event(1, "run.started", {}), "run_id": "first"})
    assert projection.snapshot["token_budget"]["used"] == 100
    assert projection.snapshot["run"]["active_seconds"] == 30
    projection.apply({**event(2, "run.started", {}), "run_id": "second"})
    assert projection.snapshot["token_budget"] == {"limit": 1000, "used": 0, "remaining": 1000}


def test_legacy_usage_stays_unknown_until_backend_reports_it():
    projection = SessionProjection({"session_id": "s", "event_cursor": 0, "messages": [], "task": {"limits": {"max_tokens": 1000}}})
    projection.apply({**event(1, "run.started", {}), "run_id": "new"})
    assert projection.snapshot["token_budget"]["used"] is None
    assert projection.snapshot["token_budget"]["remaining"] is None
    projection.apply(event(2, "run.usage.changed", {"total_tokens": 100}))
    assert projection.snapshot["token_budget"] == {"limit": 1000, "used": 100, "remaining": 900}
