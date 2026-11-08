import json

import pytest

from loom.autonomy import planner as module
from loom.core import err, make_loom_error, ok
from loom.llm.api import LlmResponse, TokenUsage
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import goals

PROPOSAL = {"work_packages": [{"objective": "Inspect the implementation", "acceptance": ["Cite source files"], "reason": "Explicit goal"}]}


class ReplyProvider:
    model = "planner-fixture"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.payloads = []

    async def chat(self, messages, tools=None, cancellation=None):
        assert tools is None
        self.payloads.append(json.loads(messages[-1].content))
        reply = next(self.replies)
        return ok(reply) if isinstance(reply, LlmResponse) else reply


def reply(content, finish_reason="stop"):
    return LlmResponse(content, usage=TokenUsage(10, 20, 30), finish_reason=finish_reason)


def configure(agent_service, monkeypatch, replies):
    service, facade, aid, _ = agent_service
    goal = facade.store.add_goal(aid, "goal", goals()[0])
    provider = ReplyProvider(replies)
    service.config_path = "fixture.yaml"
    monkeypatch.setattr(module, "load_task_config", lambda _: ok(None))
    monkeypatch.setattr(module, "_create_provider", lambda *_args, **_kwargs: ok(provider))
    planner = facade.supervisor.planner
    planner.provider = None
    return facade, facade.snapshot(aid), goal, provider


@pytest.mark.parametrize("fenced", [False, True])
def test_planner_accepts_complete_json_and_fences_preserving_goal(agent_service, monkeypatch, fenced):
    content = json.dumps(PROPOSAL)
    if fenced:
        content = "```json\n" + content + "\n```"
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [reply(content)])
    items = facade.supervisor.planner.propose(agent, goal, {})
    assert len(items) == 1
    assert goal["objective"] in items[-1]["objective"]
    assert set(goal["acceptance"]) <= set(items[-1]["acceptance"])
    assert len(provider.payloads) == 1
    budget = facade.snapshot(agent["id"])["budget"]
    assert budget[0]["calls"] == 1 and budget[0]["tokens"] == 30


@pytest.mark.parametrize("bad", [
    reply("Here is an incomplete plan"),
    reply(json.dumps({"work_packages": [{"objective": "Work", "acceptance": [], "reason": "Goal"}]})),
    reply(json.dumps(PROPOSAL), "length"),
    reply('{"work_packages": [], "work_packages": []}'),
])
def test_planner_corrects_format_once_and_accounts_each_call(agent_service, monkeypatch, bad):
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [bad, reply(json.dumps(PROPOSAL))])
    result = facade.supervisor.planner.propose(agent, goal, {})
    assert len(result) == 1
    assert len(provider.payloads) == 2
    assert provider.payloads[1]["goal"] == goal
    assert "repair" in provider.payloads[1]
    assert "response_contract" in provider.payloads[1]
    budget = facade.snapshot(agent["id"])["budget"][0]
    assert budget["calls"] == 2 and budget["tokens"] == 60


def test_exhausted_format_correction_does_not_dispatch_or_consume_goal(agent_service, monkeypatch):
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [reply("invalid"), reply("still invalid")])
    facade.supervisor.tick()
    state = facade.snapshot(agent["id"])
    assert state["goals"][0]["state"] == "queued"
    assert state["work_packages"] == [] and state["sessions"] == []
    assert "Planner response rejected (invalid_json)" in state["error"]
    assert len(provider.payloads) == 2


def test_refusal_and_transport_errors_do_not_trigger_format_retry(agent_service, monkeypatch):
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [reply("", "refusal")])
    with pytest.raises(ServiceError, match="refused"):
        facade.supervisor.planner.propose(agent, goal, {})
    assert len(provider.payloads) == 1
    provider.replies = iter([err(make_loom_error("TIMEOUT", "Request timed out", retryable=True))])
    with pytest.raises(Exception, match="Request timed out"):
        facade.supervisor.planner.propose(agent, goal, {})
    assert len(provider.payloads) == 2


def test_correction_cannot_bypass_shared_daily_budget(agent_service, monkeypatch):
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [reply("invalid"), reply(json.dumps(PROPOSAL))])
    chat = provider.chat
    async def exhaust_daily(*args, **kwargs):
        result = await chat(*args, **kwargs)
        with facade.store.sessions.transaction() as db:
            stored = facade.store.get(db, 'agents', agent['id'])
            stored['role']['budget']['daily_tokens'] = 30
            facade.store.put(db, 'agents', stored)
        return result
    provider.chat = exhaust_daily
    with pytest.raises(Exception, match="daily token budget exhausted"):
        facade.supervisor.planner.propose(agent, goal, {})
    assert len(provider.payloads) == 1


def test_read_only_planning_corrects_unavailable_tools_before_committing(agent_service, monkeypatch):
    bad = {"work_packages": [{**PROPOSAL["work_packages"][0], "required_tools": ["shell_execute"]}]}
    good = {"work_packages": [{**PROPOSAL["work_packages"][0], "required_tools": ["list_directory", "read_file"]}]}
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [reply(json.dumps(bad)), reply(json.dumps(good))])
    goal["read_only"] = True
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, "agent_goals", goal)
    facade.supervisor._plan(agent)
    work = facade.snapshot(agent["id"])["work_packages"][0]
    assert work["required_tools"] == ["list_directory", "read_file"]
    environment = provider.payloads[0]["execution_environment"]
    assert environment["read_only"]
    assert {"list_directory", "read_file", "finish"} <= set(environment["available_tools"])
    assert not {"shell_execute", "process_execute", "write_file", "edit_file"} & set(environment["available_tools"])
    assert "shell_execute" in provider.payloads[1]["repair"]["validation_error"]
    facade.supervisor.dispatcher.dispatch(agent["id"], work["id"])
    assert facade.snapshot(agent["id"])["work_packages"][0]["session_id"]
    assert facade.snapshot(agent["id"])["budget"][0]["calls"] == 2


def test_read_only_planning_rejects_repeated_capability_conflict_without_work_packages(agent_service, monkeypatch):
    bad = {"work_packages": [{**PROPOSAL["work_packages"][0], "required_tools": ["shell_execute"]}]}
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [reply(json.dumps(bad))] * 2)
    goal["read_only"] = True
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, "agent_goals", goal)
    with pytest.raises(ServiceError, match="unavailable_tools"):
        facade.supervisor._plan(agent)
    state = facade.snapshot(agent["id"])
    assert state["work_packages"] == [] and state["sessions"] == []
    assert len(provider.payloads) == 2


def test_custom_planner_cannot_bypass_read_only_tool_contract(agent_service):
    _, facade, aid, _ = agent_service
    goal = facade.store.add_goal(aid, "goal", goals()[0])
    goal["read_only"] = True
    facade.supervisor.planner.provider = lambda *_: {"work_packages": [
        {**PROPOSAL["work_packages"][0], "required_tools": ["shell_execute"]}]}
    with pytest.raises(ServiceError, match="Read-only work cannot use: shell_execute"):
        facade.supervisor.planner.propose(facade.snapshot(aid), goal, {})
