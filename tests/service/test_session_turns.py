import json

import pytest

from loom.service.controller import LoomService
from tests.service.fakes import turn_prompt_provider_factory
from tests.service.test_controller import command, wait_state


@pytest.mark.parametrize("dynamic", [False, True])
def test_follow_up_targets_latest_request_and_retains_answered_conversation_after_restart(tmp_path, dynamic):
    service = LoomService(tmp_path / "data", provider_factory=turn_prompt_provider_factory).start()
    spec = {"workflow": {"plugin": "dynamic"}, "tools": {"collections": ["task_control"]}}
    try:
        payload = {"objective": "Explain event-driven architecture", "plan_mode": "off"}
        payload.update({"task_spec": spec} if dynamic else {"workspace": str(tmp_path)})
        sid = service.create("turns", payload)["session_id"]
        first = wait_state(service, sid, "idle")
        first_answer = next(m["content"] for m in first["messages"] if m["role"] == "assistant")
        assert json.loads(first_answer)["objective"] == payload["objective"]
    finally:
        service.close()
    service = LoomService(tmp_path / "data", provider_factory=turn_prompt_provider_factory).start()
    try:
        command(service, sid, "submit_message", content="Now compare Kafka and RabbitMQ only")
        second = wait_state(service, sid, "idle")
        answer = json.loads([m["content"] for m in second["messages"] if m["role"] == "assistant"][-1])
        assert answer["objective"] == "Now compare Kafka and RabbitMQ only"
        assert second["task"]["objective"] == answer["objective"]
        assert answer["history"] == [
            {"role": "user", "content": payload["objective"]},
            {"role": "assistant", "content": first_answer},
        ]
        assert second["messages"][-2]["state"] == "applied"
        archives = [event["payload"]["artifact"] for event in service.events(sid, limit=1000) if event["type"] == "artifact.created"]
        archives = [ref for ref in archives if ref["kind"] == "session_history"]
        assert archives
        archive = json.loads(service.store.read_artifact(sid, archives[-1]["sha256"]))
        assert archive["messages"][-1]["content"] == first_answer
        if dynamic:
            assert second["workflow"]["workflow"]["nodes"][0]["objective"] == answer["objective"]
        command(service, sid, "submit_message", content="Give an example using the latter")
        third = wait_state(service, sid, "idle")
        answer = json.loads(third["messages"][-1]["content"])
        assert answer["objective"] == "Give an example using the latter"
        assert [item["role"] for item in answer["history"]] == ["user", "assistant", "user", "assistant"]
        assert third["run"]["id"] != second["run"]["id"]
    finally:
        service.close()


def test_long_history_has_a_bounded_prompt_and_keeps_the_current_request():
    from dataclasses import replace

    from loom.contexts.bounded import BoundedContextManager
    from loom.service.agent import _history
    from loom.tasks.request import TaskRequest
    from loom.tasks.runner import make_task_context

    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i) * 5000} for i in range(30)]
    history = _history(messages, 24000)
    assert len(history) <= 8
    assert sum(len(item["content"]) for item in history) <= 8000
    context = make_task_context(TaskRequest("Latest request only"), plan_mode="off").unwrap()
    context = replace(context, metadata={"session_turn": True, "session_history": history, "session_history_artifact": {"sha256": "archive"}})
    manager = BoundedContextManager({}, publish_artifact=lambda *_: {"sha256": "compaction"})
    projected = manager.project(context)
    assert projected[-1].content == "Current request for this round:\nLatest request only"
    assert any(m.role == "assistant" for m in projected)
    assert "archive" in projected[1].content
    compacted, _ = manager.compact(projected, 6000)
    assert "- Objective: Latest request only" in compacted[0].content
    assert "archive" in compacted[1].content


def test_empty_session_consumes_all_initial_messages_before_creating_workflow(tmp_path):
    service = LoomService(tmp_path / "data", provider_factory=turn_prompt_provider_factory)
    try:
        sid = service.create("empty", {"task_spec": {"workflow": {"plugin": "dynamic"}}})["session_id"]
        command(service, sid, "submit_message", content="Compare the two queues")
        command(service, sid, "submit_message", content="Include delivery guarantees")
        service.start()
        state = wait_state(service, sid, "idle")
        expected = "Compare the two queues\n\nInclude delivery guarantees"
        assert state["task"]["objective"] == expected
        assert state["workflow"]["workflow"]["nodes"][0]["objective"] == expected
        assert json.loads(state["messages"][-1]["content"])["objective"] == expected
        assert all(m["state"] == "applied" for m in state["messages"])
    finally:
        service.close()
