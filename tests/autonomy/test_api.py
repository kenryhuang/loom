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
        first = request(f"/v1/agents/{aid}/goals", body)
        assert first[0] == 200
        assert request(f"/v1/agents/{aid}/goals", body) == first
        assert request(f"/v1/agents/{aid}/commands", {"command_id": "pause", "type": "pause", "expected_revision": 1})[0] == 409
        assert request(f"/v1/agents/{aid}/context/missing")[0] == 404
        assert request(f"/v1/agents/{aid}/guidance", {"command_id": "guide", "text": "just this", "scope": "current_task"})[0] == 400
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
