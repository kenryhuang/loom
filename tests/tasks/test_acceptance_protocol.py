import asyncio
import json
from types import SimpleNamespace

import pytest

from loom.core import ok
from loom.llm import LlmResponse
from loom.tasks.acceptance import AcceptanceBlocked
from loom.tasks.acceptance_protocols import plan_protocol
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context
from tests.tasks.test_acceptance import Judge, Sink


class RecoveringJudge(Judge):
    def __init__(self, invalid_calls):
        super().__init__([
            {'id': 'command', 'description': 'Project check', 'verifier': 'command', 'scope': ['input.txt'],
             'check': {'tool_id': 'process_execute', 'input': {'argv': ['check']}}},
            {'id': 'content', 'description': 'Relevant answer', 'verifier': 'semantic', 'scope': [], 'check': {}},
        ])
        self.invalid_calls = invalid_calls
        self.verifications = 0

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        if messages[0].content.startswith('You independently verify'):
            self.verifications += 1
            payload = json.loads(messages[1].content)
            if self.verifications <= self.invalid_calls:
                # The first result looks valid, but the second cites invented evidence.
                rows = [{'criterion_id': c['id'], 'status': 'passed', 'reason': 'Supported',
                         'evidence_ids': ['candidate' if i == 0 else 'invented']} for i, c in enumerate(payload['criteria'])]
                return ok(LlmResponse(content=json.dumps({'results': rows})))
        return await super().chat(messages, tools, cancellation, **kwargs)


@pytest.mark.parametrize('change_file', [False, True])
def test_protocol_exhaustion_restores_pending_verification_without_replaying_commands(tmp_path, change_file):
    async def exercise():
        (tmp_path / 'input.txt').write_text('initial')
        request = TaskRequest('Check project', workspace=tmp_path)
        assembly = TaskAssembly(request, plan_mode='off')
        judge = RecoveringJudge(invalid_calls=2)
        assembly.configure_acceptance(judge)
        context = make_task_context(request, assembly=assembly, plan_mode='off').unwrap()
        sink = Sink()
        runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=sink, cancellation=None)
        commands = []

        async def check(criterion, runtime):
            commands.append(criterion['id'])
            return {'status': 'passed', 'reason': 'Executed check'}, {'exit_code': 0}

        assembly.acceptance.check = check
        with pytest.raises(AcceptanceBlocked, match='protocol recovery exhausted'):
            await assembly.acceptance.verify(context, runtime, 'answer')
        gate = assembly.acceptance
        assert gate.data['attempts'] == 0
        assert [r['criterion_id'] for r in gate.data['results']] == ['command']
        assert not any(e['type'] == 'verification.completed' and e['result']['method'] == 'semantic' for e in sink.events)
        assert gate.public()['protocol']['state'] == 'exhausted'
        snapshot = assembly.snapshot()
        restored = TaskAssembly(request, plan_mode='off')
        restored.configure_acceptance(judge)
        restored._local_artifacts = assembly._local_artifacts
        restored.restore(snapshot)
        restored.acceptance.check = check
        if change_file:
            (tmp_path / 'input.txt').write_text('changed')
        assert await restored.acceptance.verify(context, runtime, 'answer')
        assert commands == (['command', 'command'] if change_file else ['command'])
        assert restored.acceptance.data['attempts'] == 1
        assert restored.acceptance.data['model_calls'] == 4  # plan, two invalid responses, successful retry
        assert 'pending_verification' not in restored.acceptance.data
        await assembly.close()
        await restored.close()

    asyncio.run(exercise())


def test_optional_null_command_fields_normalize_without_weakening_required_inputs(tmp_path):
    assembly = TaskAssembly(TaskRequest('Check', workspace=tmp_path), plan_mode='off')
    protocol = plan_protocol(assembly)
    proposal = {'criteria': [{'id': 'check', 'description': 'Check file', 'verifier': 'command', 'scope': 'input.txt',
                             'check': {'tool_id': 'process_execute', 'input': {'argv': ['check'], 'cwd': None}}}]}
    normalized, changes = protocol.normalize(proposal)
    plan = assembly.acceptance.validate_plan(normalized)
    assert plan['criteria'][0]['scope'] == ['input.txt']
    assert plan['criteria'][0]['check']['input'] == {'argv': ['check']}
    assert any(r['rule'] == 'native_optional_null' for r in changes)
    normalized['criteria'][0]['check']['input']['argv'] = None
    with pytest.raises(ValueError, match='input_schema'):
        assembly.acceptance.validate_plan(normalized)


def test_native_schema_fallback_is_budgeted_cached_and_visible(tmp_path):
    from loom.llm import OpenAIProvider

    async def exercise():
        bodies = []

        async def http(url, request):
            body = request['body']
            bodies.append(body)
            if 'response_format' in body:
                return {'ok': False, 'status': 400, 'json': {'error': {'message': 'response_format json_schema is not supported'}}}
            payload = json.loads(body['messages'][1]['content'])
            if 'You design' in body['messages'][0]['content']:
                value = {'criteria': [], 'unresolved_requirements': []}
            else:
                value = {'results': [{'criterion_id': c['id'], 'status': 'passed', 'reason': 'supported', 'evidence_ids': ['candidate']}
                                     for c in payload['criteria']]}
            return {'ok': True, 'status': 200, 'json': {'choices': [{'message': {'content': json.dumps(value)}, 'finish_reason': 'stop'}],
                                                      'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}}

        provider = OpenAIProvider('test', 'qwen3.8-max', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1', http_client=http)
        assembly = TaskAssembly(TaskRequest('Answer', workspace=tmp_path), plan_mode='off')
        assembly.configure_acceptance(provider)
        context = make_task_context(assembly.request, assembly=assembly, plan_mode='off').unwrap()
        sink = Sink()
        runtime = SimpleNamespace(run_id='r', loop_id='l', trace_id='t', trace_sink=sink, cancellation=None)
        assert await assembly.acceptance.verify(context, runtime, 'answer')
        assert len(bodies) == 3  # Unsupported plan request, text plan, text verification.
        assert bodies[0]['response_format']['type'] == 'json_schema'
        assert all('response_format' not in body for body in bodies[1:])
        assert assembly.acceptance.data['model_calls'] == 3 and assembly._local_calls == 3
        assert assembly._local_usage.total_tokens == 30
        assert len([e for e in sink.events if e['type'] == 'llm.protocol.fallback']) == 1
        assert assembly.acceptance.data['attempts'] == 1
        await assembly.close()

    asyncio.run(exercise())
