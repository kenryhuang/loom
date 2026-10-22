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


@pytest.mark.parametrize('status', ['blocked', 'failed'])
@pytest.mark.asyncio
async def test_only_conclusive_failures_consume_repair_allowance(tmp_path, status):
    assembly, context, runtime = setup_gate(tmp_path)
    gate = assembly.acceptance

    async def check(criterion, runtime):
        return {'status': status, 'reason': 'Observed result'}, {}

    gate.check = check
    try:
        for _ in range(3):
            assert not await gate.verify(context, runtime, 'candidate')
        assert gate.data['attempts'] == 3
        assert gate.data['failed_attempts'] == (3 if status == 'failed' else 0)
        if status == 'failed':
            with pytest.raises(AcceptanceBlocked, match='3/3 failed attempts'):
                await gate.verify(context, runtime, 'candidate')
        else:
            assert not await gate.verify(context, runtime, 'candidate')
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
        assert gate.data['results'] == [] and gate.data['state'] == 'not_ready'
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
