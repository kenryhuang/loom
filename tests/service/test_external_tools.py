import json
import sys

import pytest

from loom.core import ok
from loom.llm.api import LlmResponse, LlmToolCall
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from tests.acceptance_fakes import FixtureVerifier
from tests.external_tools.test_external_tools import cli_config
from tests.service.test_api import api  # noqa: F401
from tests.service.test_controller import command, wait_state
from tests.service.test_session_setup import Recommender, proposal


class ExternalProvider:
    verification_provider = FixtureVerifier()
    model = 'external-test'

    def __init__(self, state):
        self.ids = state['task'].get('external_tool_ids', [])

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        names = {t['function']['name'] for t in tools or []}
        assert set(self.ids) <= names
        found = next((m for m in messages if m.name in self.ids), None)
        if self.ids and not found:
            return ok(LlmResponse('', (LlmToolCall('external-call', self.ids[0], '{"message":"hello"}'),)))
        if found:
            assert 'hello' in found.content
        return ok(LlmResponse(json.dumps({'action': {'kind': 'none', 'input': {'report': 'External result received' if found else 'No external tools'}}})))


def external_provider(state):
    return ExternalProvider(state)


def test_external_tool_runs_in_real_session_worker_and_bindings_survive_restart(tmp_path):
    service = LoomService(tmp_path/'service', provider_factory=external_provider)
    try:
        connection = service.store.external_tools.save(cli_config())
        tid = connection['tools'][0]['id']
        sid = service.create('create', {'plan_mode': 'off', 'task_spec': {'tools': {'collections': ['task_control']}}})['session_id']
        command(service, sid, 'set_external_tools', external_tool_ids=[tid])
        command(service, sid, 'submit_message', content='Use Echo to return hello')
        with pytest.raises(ServiceError, match='between tasks'):
            command(service, sid, 'set_external_tools', external_tool_ids=[])
        service.start()
        done = wait_state(service, sid, 'idle')
        assert done['messages'][-1]['content'] == 'External result received'
        events = service.events(sid, limit=1000)
        assert any(e['type'] == 'tool.completed' and e['payload']['tool_id'] == tid for e in events)
    finally:
        service.close()
    service = LoomService(tmp_path/'service', provider_factory=external_provider).start()
    try:
        assert service.snapshot(sid)['task']['external_tool_ids'] == [tid]
        command(service, sid, 'set_external_tools', external_tool_ids=[])
        command(service, sid, 'submit_message', content='Now answer without tools')
        assert wait_state(service, sid, 'idle')['messages'][-1]['content'] == 'No external tools'
    finally:
        service.close()


def test_registration_http_recommendation_manual_override_and_bindings(api):  # noqa: F811
    service, server, client, path = api
    connection = client._json('/v1/external-tools', cli_config())
    tool = connection['tools'][0]
    catalog = client._json('/v1/web/catalog')
    assert catalog['external_tools'] == [tool]
    recommender = Recommender(proposal(task_type='general', tool_collections=['task_control'], collection_reasons={},
        external_tools=[tool['id']], external_tool_reasons={tool['id']: 'Echo the message'}))
    server.session_setup.provider_factory = lambda _: recommender
    result = client._json('/v1/session-setup/recommend', {'objective': 'Echo hello'})
    assert result['external_tools'] == [tool['id']] and 'external_tools' in result['tool_collections']
    assert sys.executable not in recommender.calls[0][1].content
    sid = client.create({'external_tool_ids': result['external_tools'], 'task_spec': {'tools': {'collections': ['task_control']}}})['session_id']
    assert client.snapshot(sid)['task']['external_tool_bindings'] == [tool]
    receipt = service.store.create('external-replay', {'external_tool_ids': [tool['id']]})
    assert service.store.create('external-replay', receipt['input']) == receipt
    with pytest.raises(ServiceError, match='bindings must match'):
        service.store.create('forged-binding', {**receipt['input'], 'external_tool_bindings': []})
    plain = client.create({'task_spec': {'tools': {'collections': result['tool_collections']}}})['session_id']
    assert 'external_tools' not in client.snapshot(plain)['task']['task_spec']['tools']['collections']
    with pytest.raises(ServiceError):
        client.create({'external_tool_ids': ['invented']})
    client._json('/v1/external-tools/'+connection['id'], {**cli_config(), 'enabled': False})
    assert not client._json('/v1/external-tools')['tools']
    assert not client._json('/v1/web/catalog')['external_tools']
