import http.client
import json
import threading

import pytest

from loom.service.api import ServiceHTTPServer
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import goals


def test_authenticated_agent_api_commands_and_scope(agent_service):
    service, facade, aid, _ = agent_service
    server = ServiceHTTPServer(("127.0.0.1", 0), service, "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)

    def request(path, body=None, token="test-token"):
        connection.request(
            "POST" if body is not None else "GET",
            path,
            body=json.dumps(body) if body else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        result = connection.getresponse()
        return result.status, json.loads(result.read())

    try:
        assert request("/v1/agents", token="wrong")[0] == 401
        assert request("/v1/agents")[1]["agents"][0]["id"] == aid
        body = {"command_id": "new-goal", "goal": goals()[0]}
        assert request(f"/v1/agents/{aid}/goals", body)[0] == 409
        from tests.autonomy.test_stage_progression import setup_plan

        stage = setup_plan(facade, aid)["stages"][0]
        body = dict(command_id="stage-goal", expected_revision=stage["revision"],
                    goal={key: goals()[0][key] for key in ("objective", "acceptance")})
        path = f"/v1/agents/{aid}/stages/{stage['id']}/goals"
        first = request(path, body)
        assert first[0] == 200
        assert request(path, body) == first
        assert request(f"/v1/agents/{aid}/commands", {"command_id": "pause", "type": "pause", "expected_revision": 1})[0] == 409
        assert request(f"/v1/agents/{aid}/context/missing")[0] == 404
        assert request(f"/v1/agents/{aid}/guidance", {"command_id": "guide", "text": "just this", "scope": "current_task"})[0] == 400
        current = request(f'/v1/agents/{aid}')[1]
        settings = dict(command_id='budget-only', expected_revision=current['revision'], changes=dict(budget=dict(daily_tokens=2000000)))
        saved = request(f'/v1/agents/{aid}/configuration/budget', settings)
        assert saved[0] == 200
        assert request(f'/v1/agents/{aid}/configuration/budget', settings) == saved
        preview = request(f'/v1/agents/{aid}')[1]
        assert preview['role'] == current['role']
        assert preview['configuration']['role']['budget']['daily_tokens'] == 2000000
        assert preview['configuration']['pending']
        invalid = dict(command_id='wrong-section', expected_revision=preview['revision'], changes=dict(workspace='/tmp'))
        assert request(f'/v1/agents/{aid}/configuration/budget', invalid)[0] == 400
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_learning_library_and_owned_pending_decisions(agent_service):
    from tests.memory.helpers import seed

    service, facade, aid, path = agent_service
    sid = seed(service.store, path)
    with facade.store.sessions.transaction() as db:
        db.execute('INSERT INTO agent_sessions VALUES(?,?,NULL,0)', (sid, aid))

    def ask(state, emit, db):
        state['input_request'] = dict(id='choice-1', state='pending', kind='clarification', question='Pick a strategy', options=['A', 'B'])
        state['task']['objective'] = 'Choose a strategy'
        state['task']['state'] = 'waiting_input'

    service.store.update(sid, ask)
    other = service.store.create('other-session', {'workspace': str(path)})['session_id']
    service.store.update(other, ask)
    page = facade.dispatch('GET', [aid, 'memory', 'library'], {})
    assert page['total'] == 1 and page['records'][0]['source']['annotation_revision'] == 0
    decisions = facade.snapshot(aid)['pending_decisions']
    assert len(decisions) == 1 and decisions[0]['session_id'] == sid
    assert decisions[0]['request']['options'] == ['A', 'B']
    command = dict(command_id='answer-choice', type='answer_input', expected_task_revision=decisions[0]['task_revision'],
                   payload=dict(request_id='choice-1', answer='B, preserve the existing interface'))
    first = service.submit(sid, command)
    assert service.submit(sid, command) == first
    assert facade.snapshot(aid)['pending_decisions'] == []
    assert service.snapshot(sid)['input_request']['answer'] == command['payload']['answer']
    with pytest.raises(ServiceError, match='revision|stale'):
        service.submit(sid, {**command, 'command_id': 'stale-answer'})


def test_workspace_less_review_library_requires_an_owned_session(agent_service):
    from tests.autonomy.helpers import role
    from tests.memory.helpers import seed

    service, facade, _, path = agent_service
    agent = facade.store.create('without-workspace', {**role(path), 'workspace': None, 'source_routes': [], 'task_spec': {
        'session_environment': {'plugin': 'session', 'resources': []},
        'tools': {'collections': ['task_control']}, 'workflow': {'plugin': 'dynamic'},
    }})
    aid = agent['id']
    sid = seed(service.store, None)
    other = seed(service.store, None, evaluation='unowned')
    with facade.store.sessions.transaction() as db:
        db.execute('INSERT INTO agent_sessions VALUES(?,?,NULL,0)', (sid, aid))
    for query in ({}, {'session_id': [other]}):
        with pytest.raises(ServiceError, match='owned Session'):
            facade.dispatch('GET', [aid, 'memory', 'library'], query)
    result = facade.dispatch('GET', [aid, 'memory', 'library'], {'session_id': [sid]})
    assert result['total'] == 1
    assert result['records'][0]['source']['session_id'] == sid


def test_agent_setup_recommend_returns_mappable_proposal(agent_service):
    service, facade, _, _ = agent_service
    seen = {}

    def recommender(text, *, model, scope, extra=None):
        seen.update(text=text, model=model, scope=scope)
        return dict(
            task_type="research",
            tool_collections=["web_research", "document_outputs", "task_control"],
            rationale="研究型职责",
            collection_reasons={"web_research": "需要读取来源"},
            knowledge_base_ids=[],
            external_tools=[],
            plugins={"context": "research_context", "workflow": "dynamic"},
        )

    service.setup_recommender = recommender
    result = facade.dispatch("POST", ["setup", "recommend"], None, {"description": "跟踪竞品发布并写摘要", "model": None})
    assert seen == {"text": "跟踪竞品发布并写摘要", "model": None, "scope": "agent_init"}
    spec = result["task_spec"]
    assert spec["tools"]["collections"] == ["web_research", "document_outputs", "task_control"]
    assert spec["context"]["plugin"] == "research_context"
    assert spec["workflow"]["plugin"] == "dynamic"
    assert result["plan_mode"] == "auto"
    assert result["knowledge_base_ids"] == []
    assert result["external_tool_ids"] == []
    assert result["rationale"] == "研究型职责"
    assert result["collection_reasons"]["web_research"]


def test_agent_setup_recommend_failure_surfaces_502(agent_service):
    service, facade, _, _ = agent_service

    def recommender(text, *, model, scope, extra=None):
        raise RuntimeError("model down")

    service.setup_recommender = recommender
    with pytest.raises(ServiceError) as error:
        facade.dispatch("POST", ["setup", "recommend"], None, {"description": "整理周报", "model": None})
    assert error.value.status == 502
