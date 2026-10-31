from types import SimpleNamespace

import pytest

from loom.tasks.acceptance import DEFAULTS, AcceptanceBlocked, AcceptanceController
from loom.tasks.acceptance_budget import effective_limits
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context
from tests.tasks.test_acceptance import Judge, Sink


def setup_gate(tmp_path):
    assembly = TaskAssembly(TaskRequest('Verify candidate', workspace=tmp_path), plan_mode='off')
    assembly.configure_acceptance(Judge())
    context = make_task_context(assembly.request, assembly=assembly, plan_mode='off').unwrap()
    gate = assembly.acceptance
    gate.goal(context)
    gate.data['plan'] = gate.validate_plan({'criteria': [{'id': 'output', 'description': 'Correct deliverable', 'verifier': 'artifact',
                                                        'scope': [], 'check': {'path': 'output.txt'}}]})
    runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=Sink(), cancellation=None)
    return assembly, context, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('completed', [False, True])
async def test_resume_budget_does_not_schedule_workflow_and_feedback_does(tmp_path, completed):
    assembly, context, runtime = setup_gate(tmp_path)
    from loom.workflows.legacy import LegacyPlanningWorkflow
    workflow = LegacyPlanningWorkflow({'mode': 'force'})
    assembly.workflow = workflow
    workflow.controller.submit('Inspect source', ['Read source']).unwrap()
    item = workflow.controller.state.items[0]
    workflow.controller.update('Read source', [{'id': item.id, 'content': item.content, 'status': 'completed'}]).unwrap()
    if completed:
        workflow.controller.complete().unwrap()
    prior = workflow.controller.state.items
    assembly.acceptance.data.update(state='blocked', candidate='report', reason='Missing source evidence', model_calls=1)
    assembly.acceptance_resume_request = 'resume-repair'
    (tmp_path / 'source.txt').write_text('fresh evidence')
    try:
        await assembly.acceptance.prepare(context, runtime)
        # Budget renewal has no workflow side effects. TaskAssembly admits and
        # applies structured feedback using the existing step/control boundary.
        assert workflow.controller.state.items == prior
        assembly.acceptance.data['results'] = [{'criterion_id': 'output', 'status': 'blocked', 'reason': 'Missing source',
            'gap': {'kind': 'missing_evidence', 'targets': [{'kind': 'workspace_path', 'value': 'source.txt'}],
                    'required_fact': 'Read source content', 'suggested_action': 'Read source.txt'}}]
        result = (await assembly.acceptance_blocked_result(context, runtime, 'Missing source')).unwrap()
        assert result.control.kind == 'continue'
        assert workflow.controller.state.items[:-1] == prior
        assert workflow.controller.state.items[-1].status.value == 'in_progress'
        handlers = assembly.handlers()
        result = await handlers['read_file']({'path': 'source.txt'})
        assert result.ok and 'fresh evidence' in str(result.value)
        assert assembly._acceptance_calls[-1]['tool'] == 'read_file'
        count = len(workflow.controller.state.items)
        await assembly.acceptance.prepare(context, runtime)
        assert len(workflow.controller.state.items) == count
        assert assembly.acceptance.data['state'] != 'passed'
    finally:
        await assembly.close()


@pytest.mark.parametrize('status', ['blocked', 'failed'])
@pytest.mark.asyncio
async def test_repeated_assessment_is_cached_and_verdict_does_not_consume_repair_allowance(tmp_path, status):
    assembly, context, runtime = setup_gate(tmp_path)
    gate = assembly.acceptance

    async def check(criterion, runtime):
        return {'status': status, 'reason': 'Observed result'}, {}

    gate.check = check
    try:
        for _ in range(3):
            assert not await gate.verify(context, runtime, 'candidate')
        assert gate.data['attempts'] == 1
        assert gate.data['failed_attempts'] == (1 if status == 'failed' else 0)
        assert gate.data['repair_rounds'] == 0
        assert gate.can_repair()
    finally:
        await assembly.close()


def test_visual_defaults_cover_repair_rounds_without_overriding_explicit_limits():
    plan = {'criteria': [{'verifier': 'visual'}]}
    assert effective_limits(DEFAULTS, {}, None)['max_model_calls'] == 6
    assert effective_limits(DEFAULTS, {}, plan)['max_model_calls'] == 20
    assert effective_limits(DEFAULTS, {}, plan)['max_seconds'] == 600
    configured = effective_limits(DEFAULTS, {'max_model_calls': 2, 'max_seconds': 10}, plan)
    assert configured['max_model_calls'] == 2 and configured['max_seconds'] == 10


@pytest.mark.asyncio
async def test_budget_preflight_stops_before_paid_tools_and_reports_actual_limit(tmp_path):
    assembly, context, runtime = setup_gate(tmp_path)
    gate = assembly.acceptance
    gate.data.update(model_calls=6, elapsed_seconds=30)

    async def check(*args):
        pytest.fail('An exhausted allocation must be detected before executing checks')

    gate.check = check
    try:
        with pytest.raises(AcceptanceBlocked, match=r'model calls 6/6, time 30.0/180s'):
            await gate.verify(context, runtime, 'candidate')
        assert gate.data['failed_attempts'] == 0
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_pending_pause_does_not_consume_a_model_request(tmp_path):
    assembly, _, runtime = setup_gate(tmp_path)
    assembly.execution = SimpleNamespace(poll_control=lambda: {'reason': 'pause'})
    try:
        with pytest.raises(AcceptanceBlocked, match='pause'):
            await assembly.acceptance.model(runtime, 'visual', 'Prompt', {})
        assert assembly.acceptance.data['model_calls'] == 0
    finally:
        await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['blocked', 'not_ready'])
async def test_explicit_resume_archives_allocation_and_is_idempotent_after_checkpoint_restore(tmp_path, state):
    assembly, context, runtime = setup_gate(tmp_path)
    gate = assembly.acceptance
    previous = [{'criterion_id': 'output', 'status': 'failed', 'reason': 'Still incorrect'}]
    gate.data.update(state=state, reason='Budget exhausted', results=previous, attempts=3, failed_attempts=1,
                     model_calls=6, elapsed_seconds=109, pending_operations={'output': 'uncertain-op'})
    plan, goal = gate.snapshot()['plan'], gate.data['goal_digest']
    assembly._local_calls = 9
    assembly.acceptance_resume_request = 'explicit-resume'
    try:
        await gate.prepare(context, runtime)
        assert gate.data['plan'] == plan and gate.data['goal_digest'] == goal
        assert gate.data['results'] == previous and gate.data['state'] == 'not_ready'
        assert gate.data['model_calls'] == gate.data['attempts'] == gate.data['failed_attempts'] == 0
        assert assembly._local_calls == 9  # Task-wide paid usage is not reset.
        assert gate.data['pending_operations'] == {'output': 'uncertain-op'}
        archive = assembly.read_artifact(gate.data['budget_history'][0]['sha256'])
        assert archive['results'] == previous
        assert archive['budget']['model_calls'] == 6 and archive['budget']['elapsed_seconds'] == 109
        assert gate.public()['budget']['total_model_calls'] == 6
        assert sum(e['type'] == 'acceptance.budget.renewed' for e in runtime.trace_sink.events) == 1
        saved = gate.snapshot()
        restored = AcceptanceController(assembly)
        restored.restore(saved)
        restored.data.update(state='blocked', model_calls=1)
        await restored.prepare(context, runtime)
        assert restored.data['model_calls'] == 1 and restored.data['budget_generation'] == 1
        assembly.acceptance_limits = {'max_llm_calls': 9}
        with pytest.raises(AcceptanceBlocked, match='Task model budget exhausted'):
            await gate.model(runtime, 'verify', 'Prompt', {})
        assert assembly._local_calls == 9
    finally:
        await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('configured,elapsed,expected', [({}, 0, 120), ({'model_timeout_seconds': 45}, 0, 45), ({}, 150, 30)])
async def test_model_timeout_respects_request_limit_and_remaining_allocation(tmp_path, configured, elapsed, expected):
    from loom.llm import LlmResponse

    assembly, _, runtime = setup_gate(tmp_path)
    controller = assembly.acceptance
    controller._configured_limits.update(configured)
    controller.data['elapsed_seconds'] = elapsed
    observed = []

    async def request(_runtime, _stage, _prompt, _content, timeout, **_kwargs):
        observed.append(timeout)
        return LlmResponse('{"ok":true}')

    assembly.acceptance_model_request = request
    try:
        assert await controller.model(runtime, 'plan', 'Return JSON', {}) == {'ok': True}
        assert observed == [expected]
        assert controller.data['model_calls'] == 1
    finally:
        await assembly.close()


@pytest.mark.parametrize('model,options,expected', [
    ('qwen3.8-max', {'enable_thinking': True}, 4096),
    ('qwen3.8-max-0902', {'enable_thinking': True}, 4096),
    ('qwen3.8-max', {'enable_thinking': True, 'thinking_budget': 2000}, 2000),
    ('qwen3.8-max', {'enable_thinking': True, 'reasoning_effort': 'high'}, None),
    ('qwen3.8-max', {'enable_thinking': False}, None),
    ('another-model', {'enable_thinking': True}, None),
])
def test_acceptance_thinking_budget_preserves_explicit_options(model, options, expected):
    from loom.llm.api import OpenAIProvider
    from loom.tasks.acceptance_budget import reserve_answer_budget

    original = OpenAIProvider(api_key='fixture', model=model, max_completion_tokens=8192, request_options=options)
    bounded = reserve_answer_budget(original)
    assert bounded.request_options.get('thinking_budget') == expected
    assert dict(original.request_options) == options
    assert bounded.max_completion_tokens == 8192


@pytest.mark.asyncio
async def test_acceptance_timeout_records_nonempty_failure_and_effective_request_limit(tmp_path):
    import asyncio

    assembly, _, runtime = setup_gate(tmp_path)
    class SlowJudge:
        model = 'slow-judge'

        async def chat(self, *args, **kwargs):
            await asyncio.Event().wait()

    assembly.configure_acceptance(SlowJudge())
    try:
        with pytest.raises(AcceptanceBlocked, match=r'Acceptance verify request timed out after 0.0s'):
            await assembly.acceptance_model_request(runtime, 'verify', 'Return JSON', '{}', 0.01)
        failed = next(e for e in runtime.trace_sink.events if e['type'] == 'llm.failed')
        assert failed['error']['code'] == 'TIMEOUT'
        assert failed['error']['timeout_seconds'] == 0.01
        assert 'model=slow-judge' in failed['error']['message']
        assert 'output_limit=4096 tokens' in failed['error']['message']
    finally:
        await assembly.close()
