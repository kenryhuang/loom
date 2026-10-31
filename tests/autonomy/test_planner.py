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


def test_correction_cannot_bypass_call_budget(agent_service, monkeypatch):
    facade, agent, goal, provider = configure(agent_service, monkeypatch, [reply("invalid"), reply(json.dumps(PROPOSAL))])
    with facade.store.sessions.transaction() as db:
        stored = facade.store.get(db, "agents", agent["id"])
        stored["role"]["budget"]["max_calls"] = 1
        facade.store.put(db, "agents", stored)
    with pytest.raises(Exception, match="call budget exhausted"):
        facade.supervisor.planner.propose(agent, goal, {})
    assert len(provider.payloads) == 1
