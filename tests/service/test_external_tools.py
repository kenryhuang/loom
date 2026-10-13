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


class PausedBindingProvider(ExternalProvider):
    def __init__(self, state):
        super().__init__(state)
        self.updated = bool(state['run'].get('assembly_update_pending'))

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        import asyncio
        if self.updated:
            assert 'knowledge_search' in {t['function']['name'] for t in tools}
            assert any(m.name in self.ids and 'hello' in m.content for m in messages)
            if not any(m.tool_call_id == 'updated-call' for m in messages):
                return ok(LlmResponse('', (LlmToolCall('updated-call', self.ids[0], '{"message":"updated"}'),)))
            return ok(LlmResponse(json.dumps({'action': {'kind': 'none', 'input': {'report': 'Updated tools used'}}})))
        if any(m.name in self.ids for m in messages):
            await asyncio.sleep(60)
        return await super().chat(messages, tools, cancellation, tool_choice)


def paused_binding_provider(state):
    return PausedBindingProvider(state)


@pytest.mark.parametrize('workflow', ['legacy_planning', 'dynamic'])
def test_paused_rebind_restores_progress_and_uses_updated_assembly(tmp_path, workflow):
    import time

    from loom.runtime.checkpoints import decode

    service = LoomService(tmp_path/'service', provider_factory=paused_binding_provider).start()
    try:
        connection = service.store.external_tools.save(cli_config())
        tid = connection['tools'][0]['id']
        kb = service.store.knowledge.create({'name': 'Manual', 'engine': 'sqlite_fts'})
        workflow_spec = {'plugin': workflow, **({'mode': 'off'} if workflow == 'legacy_planning'
                                               else {'nodes': [{'id': 'work', 'objective': 'Echo hello'}]})}
        sid = service.create('paused', {'objective': 'Echo hello', 'external_tool_ids': [tid],
            'plan_mode': 'off', 'task_spec': {'tools': {'collections': ['task_control']}, 'workflow': workflow_spec}})['session_id']
        deadline = time.monotonic() + 10
        while not any(e['type'] == 'tool.completed' and e['payload'].get('tool_id') == tid for e in service.events(sid, limit=1000)):
            assert time.monotonic() < deadline
            time.sleep(.03)
        command(service, sid, 'pause')
        paused = wait_state(service, sid, 'paused')
        while sid in service.active:
            assert time.monotonic() < deadline
            time.sleep(.03)
        original = service.store.snapshot(sid)
        old_cp = service._checkpoint_value(original)
        updated = service.store.external_tools.save(cli_config(timeout_seconds=61), connection['id'])
        command(service, sid, 'set_external_tools', external_tool_ids=[tid])
        command(service, sid, 'set_knowledge_bases', knowledge_base_ids=[kb['id']])
        rebound = service.store.snapshot(sid)
        assert rebound['task']['state'] == 'paused'
        assert rebound['context'] == original['context']
        assert rebound['run']['checkpoint'] == original['run']['checkpoint']
        assert rebound['task']['external_tool_bindings'] == updated['tools']
        command(service, sid, 'resume')
        done = wait_state(service, sid, 'idle')
        assert done['run']['id'] == paused['run']['id']
        assert done['messages'][-1]['content'] == 'Updated tools used'
        saved = service.store.snapshot(sid)
        assert 'assembly_update_pending' not in saved['run']
        assert decode(saved['run']['counters'])['llm_calls'] >= old_cp['llm_calls']
        completed = [e['payload'] for e in service.events(sid, limit=1000) if e['type'] == 'tool.completed' and e['payload'].get('tool_id') == tid]
        assert len(completed) == 2
    finally:
        service.close()


@pytest.mark.parametrize('guard', ['recovery', 'workspace', 'worker', 'workflow'])
def test_paused_tool_update_rejects_unsafe_boundaries_without_mutation(tmp_path, guard):
    service = LoomService(tmp_path/'service')
    try:
        connection = service.store.external_tools.save(cli_config())
        tid = connection['tools'][0]['id']
        sid = service.create('guard', {'external_tool_ids': [tid]})['session_id']
        def suspend(state, emit, db):
            state['task']['state'] = 'paused'
            state['run'] = {'id': 'run', 'state': 'suspended'}
            if guard == 'recovery':
                state['input_request'] = {'state': 'pending', 'kind': 'recovery'}
            if guard == 'workspace':
                state['workspace_blocked'] = ['process']
            if guard == 'workflow':
                state['workflow'] = {'workflow': {'nodes': [{'id': 'lookup', 'executor_config': {'allowed_tools': [tid]}}]}}
        service.store.update(sid, suspend)
        if guard == 'worker':
            service.active[sid] = object()
        previous = service.store.snapshot(sid)
        with pytest.raises(ServiceError):
            command(service, sid, 'set_external_tools', external_tool_ids=[])
        assert service.store.snapshot(sid) == previous
    finally:
        service.active.clear()
        service.close()
