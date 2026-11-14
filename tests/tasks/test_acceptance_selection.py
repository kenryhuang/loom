"""Bounded evidence selection is independent of the final acceptance verdict."""

import json

import pytest

from loom.core import ok
from loom.llm import LlmResponse, OpenAIProvider
from loom.tasks.acceptance import AcceptanceBlocked
from loom.tasks.assembly import TaskAssembly
from tests.tasks.test_acceptance import Judge
from tests.tasks.test_acceptance_batches import criterion, passed, setup
from tests.tasks.test_acceptance_budget import setup_gate


def add_sources(assembly, count=40):
    for i in range(count):
        ref = assembly.publish_artifact({'output': {'stdout': f'original source {i}: ' + '证' * 3000}}, 'acceptance_tool_evidence')
        assembly._acceptance_calls.append(dict(id=f'tool:source-{i}', tool='shell_execute', ok=True,
            goal_digest=assembly.acceptance.data['goal_digest'], paths=[f'src/{i}.ts'], input=f'cat src/{i}.ts',
            artifact=ref, artifact_format='call'))


@pytest.mark.asyncio
async def test_large_shared_sources_are_selected_once_and_rejection_cannot_pass(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(i, scope=[]) for i in range(3)], {'max_evidence_pages': 4})
    add_sources(assembly)
    requests = []

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        requests.append((stage, payload))
        if stage == 'select_evidence':
            assert len(payload['source_catalogue']) == 40
            assert len(payload['criteria']) == 3
            return LlmResponse(json.dumps({'evidence_ids': ['tool:source-0'], 'reason': 'Shared relevant source'}))
        assert stage == 'verify'
        assert len(payload['criteria']) == 3
        assert next(r for r in payload['evidence'] if r['id'] == 'tool:source-0')['output'].count('证') == 3000
        coverage = next(r for r in payload['evidence'] if r['id'] == 'tool-call-coverage')
        assert coverage['reviewed_calls'] == 1 and coverage['omitted_calls'] == 39
        selection = next(r for r in payload['evidence'] if r['id'] == 'evidence-selection')
        assert len(selection['omitted_sources']) == 39
        result = passed(payload)
        result['results'][0].update(status='failed', reason='Original source contradicts the candidate',
            gap={'kind': 'deliverable_mismatch', 'targets': [], 'required_fact': 'Correct result', 'suggested_action': 'Repair result'})
        return LlmResponse(json.dumps(result))

    assembly.acceptance_model_request = request
    try:
        assert not await assembly.acceptance.verify(context, runtime, 'candidate')
        assert [s for s, _ in requests] == ['select_evidence', 'verify']
        assert assembly.acceptance.data['state'] == 'needs_repair'
        assert len(assembly._acceptance_calls) == 40
    finally:
        await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted_criterion', ['c0', 'goal'])
async def test_selection_and_completed_judgment_survive_restore_without_reselecting(tmp_path, interrupted_criterion):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, scope=[])])
    add_sources(assembly)
    requests = []
    interrupted = True

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        payload = json.loads(content)
        ids = [c['id'] for c in payload['criteria']]
        requests.append((stage, ids))
        if stage == 'select_evidence':
            return LlmResponse(json.dumps({'evidence_ids': ['tool:source-0'], 'reason': 'Relevant evidence'}))
        if ids == [interrupted_criterion] and interrupted:
            raise TimeoutError()
        return LlmResponse(json.dumps(passed(payload)))

    assembly.acceptance_model_request = request
    restored = None
    try:
        with pytest.raises(AcceptanceBlocked):
            await assembly.acceptance.verify(context, runtime, 'candidate')
        snapshot = assembly.snapshot()
        restored = TaskAssembly(assembly.request, plan_mode='off')
        restored.configure_acceptance(Judge())
        restored._local_artifacts = assembly._local_artifacts
        restored.restore(snapshot)
        restored.acceptance_model_request = request
        interrupted = False
        assert await restored.acceptance.verify(context, runtime, 'candidate')
        assert requests.count(('select_evidence', ['c0'])) == 1
        assert requests.count(('verify', ['c0'])) == (2 if interrupted_criterion == 'c0' else 1)
        assert requests.count(('select_evidence', ['goal'])) == 0
        assert requests.count(('verify', ['goal'])) == (2 if interrupted_criterion == 'goal' else 1)
    finally:
        await assembly.close()
        if restored:
            await restored.close()


@pytest.mark.asyncio
async def test_selection_rejects_invented_source_ids(tmp_path):
    assembly, context, runtime = await setup(tmp_path, [criterion(0, scope=[])])
    add_sources(assembly)

    async def request(runtime, stage, prompt, content, timeout, **kwargs):
        assert stage.startswith('select_evidence')
        return LlmResponse(json.dumps({'evidence_ids': ['invented'], 'reason': 'Unsupported'}))

    assembly.acceptance_model_request = request
    try:
        with pytest.raises(AcceptanceBlocked):
            await assembly.acceptance.verify(context, runtime, 'candidate')
        assert assembly.acceptance.data['state'] != 'passed'
        assert not assembly.acceptance.data['pending_verification']['selections']
    finally:
        await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage,configured,expected', [('select_evidence', {}, 4096), ('verify_page', {}, 4096), ('verify_page_repair', {}, 4096),
    ('verify', {}, 8192), ('verify', {'review_max_output_tokens': 12000}, 12000), ('verify_page', {'max_output_tokens': 1024}, 1024)])
async def test_stage_output_budget_bounds_thinking_without_changing_solver(tmp_path, monkeypatch, stage, configured, expected):
    from loom.llm.provider_wrappers import unwrap_provider

    assembly, _, runtime = setup_gate(tmp_path)
    original = OpenAIProvider(api_key='fixture', model='qwen3.8-max', max_completion_tokens=65536,
                              request_options={'enable_thinking': True})
    assembly.configure_acceptance(original)
    assembly.acceptance._configured_limits.update(configured)

    async def request(provider, *args, **kwargs):
        bounded = unwrap_provider(provider)
        assert bounded.max_completion_tokens == expected
        assert bounded.request_options['thinking_budget'] == expected // 2
        return ok(LlmResponse('{}'))

    monkeypatch.setattr('loom.llm.request_llm_response', request)
    try:
        await assembly.acceptance_model_request(runtime, stage, 'Review', '{}', 10)
        assert original.max_completion_tokens == 65536
        assert 'thinking_budget' not in original.request_options
    finally:
        await assembly.close()
