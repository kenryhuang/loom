import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from loom.autonomy.reassembly import validate_review
from loom.autonomy.recovery_workspace import preservation_evidence
from loom.tasks.recovery import assess
from tests.autonomy.test_acceptance_recovery import acceptance_blocked_work


def blocked(agent_service, *, readonly=False, budget=False):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    objective = 'Run the required test command and record actual output. Preserve all original files including existing uncommitted changes.'
    criteria = [dict(id='test', description='Required command output is recorded', verifier='semantic', scope=[], check={}, required=True),
                dict(id='preserve', description='Git tree is clean', verifier='semantic', scope=[], check={}, required=True),
                dict(id='goal', description='Full original goal is satisfied', verifier='semantic', scope=[], check={}, required=True)]

    def update(state, emit, db):
        state['task']['objective'] = objective
        spec = state['task']['task_spec']
        spec['tools']['collections'] = ['filesystem', 'task_control']
        if readonly:
            spec['session_environment']['resources'][0]['access'] = 'read-only'
        state['acceptance'] = dict(state='blocked', reason='任意提示', plan=dict(criteria=criteria, unresolved_requirements=[]),
            results=[dict(criterion_id='test', status='failed', gap=dict(kind='missing_evidence',
                     targets=[dict(kind='tool', value='process_execute')], required_fact='Recorded execution', suggested_action='Execute'))],
            decision=dict(reason_code='ACCEPTANCE_CAPABILITY_UNAVAILABLE', assessment_id='a1'))
        if budget:
            state['acceptance']['budget'] = dict(model_calls=6, max_model_calls=6, elapsed_seconds=1, max_seconds=180)
    service.store.update(sid, update)

    def reviewer(payload):
        return dict(reviews=[dict(criterion_id=c['id'], relation='overreach' if c['id'] == 'preserve' else 'supported',
            requirement_quotes=[objective],
            description='Original files preserved relative to the starting state' if c['id'] == 'preserve' else c['description'],
            reason='Compare with the original requirement') for c in payload['criteria']])
    service.recovery_reviewer = reviewer
    return service, facade, aid, work, sid


def test_combined_tools_budget_and_contract_recovery_is_durable_and_rebuilds(agent_service):
    service, facade, aid, work, sid = blocked(agent_service, budget=True)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'recovered', snap['scheduler']
    state = service.store.snapshot(sid)
    assert state['task']['state'] == 'queued' and state['run'] is None and state['context'] is None
    assert 'shell' in state['task']['task_spec']['tools']['collections']
    assert state['task']['task_spec']['acceptance']['max_model_calls'] == 12
    recovery = state['recovery_context']
    assert recovery['acceptance_plan']['criteria'][1]['description'] == 'Original files preserved relative to the starting state'
    assert recovery['audit']['original_plan']['criteria'][1]['description'] == 'Git tree is clean'
    history = snap['work_packages'][0]['recovery']['history']
    assert {c['code'] for c in history[0]['report']['causes']} >= {'ACCEPTANCE_BUDGET', 'TOOLS_REQUIRED', 'ACCEPTANCE_REVIEW'}
    assert history[0]['report']['reset'] == ['workflow', 'plan', 'acceptance', 'context']
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)['work_packages'][0]['recovery']['attempts'] == 1


def test_readonly_recovery_runs_real_commands_on_working_copy_and_preserves_dirty_source(agent_service):
    service, facade, aid, work, sid = blocked(agent_service, readonly=True)
    source = Path(service.store.snapshot(sid)['task']['workspace'])
    import subprocess

    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source / '.gitignore').write_text('dist/\n')
    (source / 'uncommitted.txt').write_text('local changes must survive')
    def command_check(state, emit, db):
        criterion = state['acceptance']['plan']['criteria'][0]
        criterion.update(verifier='command', check=dict(tool_id='process_execute', input=dict(argv=['cat', str(source / 'uncommitted.txt')])))
    service.store.update(sid, command_check)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'recovered', snap['scheduler']
    state = service.store.snapshot(sid)
    workspace = Path(state['task']['workspace'])
    assert workspace != source and (workspace / 'uncommitted.txt').read_text() == 'local changes must survive'
    recovery = state['recovery_context']
    assert recovery['acceptance_plan']['criteria'][0]['check']['input']['argv'][-1] == str(workspace / 'uncommitted.txt')
    assert recovery['audit']['original_plan']['criteria'][0]['check']['input']['argv'][-1] == str(source / 'uncommitted.txt')
    from loom.tasks.request import TaskRequest
    from loom.tasks.tools import make_task_tools

    tool = make_task_tools(TaskRequest(state['task']['objective'], workspace=workspace))['process_execute']
    import sys

    script = "import os; os.mkdir('dist'); open('dist/validation-output','w').write('tested'); print('exit evidence')"
    result = asyncio.run(tool(dict(argv=[sys.executable, '-c', script])))
    assert result.ok
    assert (workspace / 'dist/validation-output').exists() and not (source / 'dist/validation-output').exists()
    assert preservation_evidence(state['recovery_context']['workspace_copy'])['unchanged']
    assert (source / 'uncommitted.txt').read_text() == 'local changes must survive'
    (workspace / 'uncommitted.txt').write_text('fake fix in copy')
    assert not preservation_evidence(state['recovery_context']['workspace_copy'])['unchanged']


def test_same_collection_does_not_imply_tool_available_under_readonly_policy(agent_service):
    service, facade, aid, work, sid = blocked(agent_service, readonly=True)
    service.store.update(sid, lambda state, emit, db: state['task']['task_spec']['tools']['collections'].append('shell'))
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)['scheduler']['state'] == 'recovered'
    assert service.store.snapshot(sid)['recovery_context']['workspace_copy']


def test_pure_acceptance_budget_renews_without_discarding_execution_plan(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    def update(state, emit, db):
        state['acceptance'] = dict(state='blocked', cause_code='ACCEPTANCE_BUDGET_EXHAUSTED', reason='任意文字',
            results=[], plan=dict(criteria=[], unresolved_requirements=[]), budget=dict(model_calls=6, max_model_calls=6))
        state['plan'] = dict(phase='executing', items=[])
    service.store.update(sid, update)
    assert assess(service.store.snapshot(sid))['mode'] == 'resume'
    facade.supervisor.scheduler.advance(aid, manual=True)
    state = service.store.snapshot(sid)
    assert state['task']['state'] == 'queued'
    assert state['plan']['phase'] == 'executing'
    assert state['run']['acceptance_budget_request']


@pytest.mark.parametrize('cause', ['EXECUTION_UNKNOWN', 'ACCEPTANCE_EXTERNAL_DEPENDENCY'])
def test_unknown_execution_and_external_dependency_never_reassemble(agent_service, cause):
    service, facade, aid, work, sid = blocked(agent_service)
    service.store.update(sid, lambda state, emit, db: state['acceptance']['decision'].update(reason_code=cause))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'intervention'
    assert not snap['work_packages'][0].get('recovery', {}).get('attempts')
    assert service.store.snapshot(sid)['task']['state'] == 'paused'


def test_human_change_during_review_invalidates_prepared_reassembly(agent_service):
    service, facade, aid, work, sid = blocked(agent_service)
    reviewer = service.recovery_reviewer
    def interrupt(payload):
        service.store.update(sid, lambda state, emit, db: state.update(control=dict(command_id='human', kind='paused')))
        return reviewer(payload)
    service.recovery_reviewer = interrupt
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'intervention'
    assert not snap['work_packages'][0].get('recovery', {}).get('attempts')
    assert 'recovery_context' not in service.store.snapshot(sid)


def test_review_cannot_silently_change_supported_conditions_or_cite_invented_requirements():
    payload = dict(original_objective='Keep original files', criteria=[dict(id='c', description='Preserve files')])
    row = dict(criterion_id='c', relation='supported', requirement_quotes=['Keep original files'], description='Preserve files', reason='Grounded')
    assert validate_review(dict(reviews=[row]), payload)
    for changes in [dict(description='Delete files'), dict(requirement_quotes=['Invented permission'])]:
        with pytest.raises(ValueError):
            validate_review(dict(reviews=[dict(row, **changes)]), payload)


def test_recovery_classification_does_not_read_error_messages(agent_service):
    service, facade, aid, work, sid = blocked(agent_service)
    state = service.store.snapshot(sid)
    original = assess(state)['causes']
    changed = deepcopy(state)
    changed['acceptance']['reason'] = 'Token budget exhausted; clean git status; error'
    changed['run']['reason'] = 'Active time budget exceeded'
    assert assess(changed)['causes'] == original


def test_explicit_reassessment_grants_one_attempt_without_erasing_history(agent_service):
    service, facade, aid, work, sid = blocked(agent_service)
    from loom.autonomy.contracts import scheduling_policy

    limit = scheduling_policy(facade.snapshot(aid)['role'])['max_recovery_attempts']
    with service.store.transaction() as db:
        current = facade.store.get(db, 'agent_work_packages', work['id'], aid)
        current['recovery'] = dict(attempts=limit, exhausted=True, history=[dict(id='old-failure')])
        facade.store.put(db, 'agent_work_packages', current)
    snap = facade.snapshot(aid)
    payload = dict(work_package_id=work['id'], reason='Corrected recovery implementation')
    first = facade.store.command(aid, 'explicit-recovery', 'recover', snap['revision'], payload)
    assert facade.store.command(aid, 'explicit-recovery', 'recover', snap['revision'], payload) == first
    recovery = facade.snapshot(aid)['work_packages'][0]['recovery']
    assert recovery['attempts'] == limit and recovery['attempt_limit'] == limit + 1
    assert recovery['history'][0]['id'] == 'old-failure' and len(recovery['manual_grants']) == 1
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'recovered'
    assert snap['work_packages'][0]['recovery']['attempts'] == limit + 1


def test_plan_token_block_reassembles_with_the_granted_task_budget(agent_service):
    from tests.autonomy.test_queue_scheduling import paused_work

    service, facade, aid, work, sid = paused_work(agent_service)
    before = service.store.snapshot(sid)['task']['limits']['max_tokens']
    def update(state, emit, db):
        state.pop('acceptance', None)
        state['run']['stop_code'] = 'PLAN_BLOCKED'
        state['plan'] = dict(items=[dict(id='step', status='blocked', blocker=dict(code='TOKEN_BUDGET'))])
    service.store.update(sid, update)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)['scheduler']['state'] == 'recovered'
    state = service.store.snapshot(sid)
    assert state['task']['limits']['max_tokens'] > before
    assert 'plan' not in state


def test_restart_cannot_attach_reviewed_contract_to_a_different_prepared_task(agent_service):
    from loom.service.contracts import ServiceError
    from loom.tasks.workspace_probe import digest

    service, facade, aid, work, sid = blocked(agent_service)
    state = service.store.snapshot(sid)
    context = dict(command_id='prepared', source_run_id=state['run']['id'], task_key=digest(dict(objective='Original task')))
    service.store.update(sid, lambda s, emit, db: s.update(pending_reassembly=context))
    with pytest.raises(ServiceError, match='prepared task'):
        service.submit(sid, dict(command_id='prepared', type='restart_task', payload=dict(task=dict(objective='Different task'))))
    assert service.store.snapshot(sid)['task']['state'] == 'paused'
    assert not service.store.snapshot(sid).get('recovery_context')


def test_budget_denial_during_contract_review_waits_without_consuming_recovery(agent_service):
    from loom.service.contracts import ServiceError

    service, facade, aid, work, sid = blocked(agent_service)
    def denied(payload):
        error = ServiceError('Any display wording', 409, code='AGENT_BUDGET')
        error.budget_block = dict(scopes=[dict(scope='daily', used=95, limit=100, requested=20)])
        raise error
    service.recovery_reviewer = denied
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert not snap['scheduler']['manual_required']
    assert snap['scheduler']['blockers'][0]['code'] == 'EXECUTION_BUDGET'
    recovery = snap['work_packages'][0]['recovery']
    assert not recovery.get('attempts', 0)
    assert recovery['last_failure']['error_code'] == 'AGENT_BUDGET'


def test_review_provider_budget_error_preserves_serializable_scope_details(agent_service):
    import json

    from loom.autonomy.reassembly import review_contract
    from loom.core import err, make_loom_error
    from loom.service.contracts import ServiceError

    service, facade, aid, work, sid = blocked(agent_service)
    state = service.store.snapshot(sid)
    state.pop('autonomy')
    del service.recovery_reviewer
    class DeniedProvider:
        async def chat(self, messages):
            return err(make_loom_error('AGENT_BUDGET', 'Budget denied', retryable=True, cause=dict(scopes=[dict(scope='daily')])))
    service.setup_provider_factory = lambda model: DeniedProvider()
    with pytest.raises(ServiceError) as failure:
        review_contract(service, state, assess(state))
    assert json.loads(json.dumps(failure.value.budget_block)) == dict(scopes=[dict(scope='daily')])
