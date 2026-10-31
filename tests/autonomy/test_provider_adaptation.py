import json
from types import SimpleNamespace

import pytest

from loom.autonomy.budget import BudgetLedger, BudgetProvider
from loom.evaluation.provider import evaluator_provider
from loom.llm.api import OpenAIProvider
from loom.llm.provider_wrappers import map_provider, unwrap_provider
from loom.llm.structured import StructuredProtocol, output_mode, protocol_provider
from loom.tasks.acceptance import AcceptanceBlocked
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from tests.tasks.test_acceptance import Sink

PROTOCOL = StructuredProtocol('agent_acceptance_fixture', 1, {
    'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False,
})


@pytest.mark.asyncio
@pytest.mark.parametrize('denied', [False, True])
async def test_acceptance_retains_native_format_limits_and_agent_budget(agent_service, denied):
    service, facade, aid, path = agent_service
    requests = []

    async def http(_url, request):
        requests.append(request['body'])
        return {'ok': True, 'json': {
            'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30},
        }}

    original = OpenAIProvider(api_key='fixture', model='qwen3.8-max', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
                              max_completion_tokens=65536, request_options={'enable_thinking': True}, http_client=http)
    ledger = BudgetLedger(service.store.path)
    wrapped = BudgetProvider(original, ledger, {'agent_id': aid, 'goal_id': 'g', 'work_package_id': 'w'}, category='acceptance')
    assembly = TaskAssembly(TaskRequest('Verify candidate', workspace=path), plan_mode='off')
    assembly.configure_acceptance(wrapped)
    runtime = SimpleNamespace(run_id='r', loop_id='l', trace_id='t', trace_sink=Sink(), cancellation=None)
    if denied:
        with service.store.transaction() as db:
            agent = facade.store.get(db, 'agents', aid)
            agent['role']['budget']['daily_tokens'] = 1
            facade.store.put(db, 'agents', agent)
    try:
        call = assembly.acceptance.structured(runtime, 'plan', 'Return JSON', {}, PROTOCOL, lambda value: value)
        if denied:
            with pytest.raises(AcceptanceBlocked, match='budget exhausted'):
                await call
            assert requests == []
        else:
            assert await call == {'ok': True}
            assert len(requests) == 1
            assert requests[0]['response_format']['type'] == 'json_schema'
            assert requests[0]['max_completion_tokens'] == 8192
            assert requests[0]['enable_thinking'] is True
            assert requests[0]['thinking_budget'] == 4096
            assert 'response_contract' not in json.loads(requests[0]['messages'][-1]['content'])
            budget = facade.snapshot(aid)['budget'][0]
            assert (budget['category'], budget['calls'], budget['tokens']) == ('acceptance', 1, 30)
        assert wrapped.provider.max_completion_tokens == 8192
        assert original.max_completion_tokens == 65536
        assert 'response_format' not in original.request_options
        assert 'thinking_budget' not in original.request_options
    finally:
        await assembly.close()


def test_transport_and_image_adaptation_preserve_the_accounting_wrapper():
    original = OpenAIProvider(api_key='fixture', model='qwen3.8-max', max_completion_tokens=65536,
                              request_options={'enable_thinking': True, 'response_format': {'type': 'json_object'}})
    ledger, binding = object(), {'agent_id': 'a'}
    wrapped = BudgetProvider(original, ledger, binding, category='acceptance')
    bounded = evaluator_provider(wrapped, output_tokens=4096, timeout_seconds=120)
    prepared = protocol_provider(bounded, PROTOCOL, output_mode(bounded))
    assert isinstance(prepared, BudgetProvider)
    assert prepared.ledger is ledger and prepared.binding is binding
    assert prepared.category == 'acceptance'
    assert prepared.provider.http_client is not None
    assert prepared.provider.max_completion_tokens == 4096
    assert output_mode(prepared) == 'json_object'
    assert original.http_client is None and wrapped.provider.http_client is None
    from dataclasses import replace

    def reader(ref):
        return ref
    visual = map_provider(prepared, lambda base: replace(base, image_reader=reader))
    assert visual.provider.image_reader is reader
    assert unwrap_provider(visual) is visual.provider
    assert isinstance(visual, BudgetProvider) and visual.ledger is ledger
