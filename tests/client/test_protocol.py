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
