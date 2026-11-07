from copy import deepcopy

import pytest

from tests.autonomy.test_acceptance_recovery import acceptance_blocked_work


def capability_blocked(agent_service):
    service, facade, aid, work, sid = acceptance_blocked_work(agent_service)
    with service.store.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['role']['scheduling_policy']['tick_seconds'] = 900
        agent['next_schedule_at'] = '2099-01-01T00:00:00+00:00'
        facade.store.put(db, 'agents', agent)
    def update(state, emit, db):
        state['task']['task_spec']['tools']['collections'] = ['filesystem', 'task_control']
        state['run'].update(stop_code='ACCEPTANCE_CAPABILITY_UNAVAILABLE', active_seconds=20)
        state['acceptance'] = dict(state='blocked', cause_code='ACCEPTANCE_CAPABILITY_UNAVAILABLE', plan=None,
            results=[dict(criterion_id='execution-capabilities', status='blocked', gap=dict(kind='missing_evidence',
                targets=[dict(kind='tool', value='shell_execute')]))])
        state['plan'] = dict(items=[dict(id='read', status='completed'), dict(id='validate', status='pending')])
    service.store.update(sid, update)
    return service, facade, aid, work, sid


def test_missing_tools_wake_immediately_and_resume_without_resetting_task(agent_service):
    service, facade, aid, work, sid = capability_blocked(agent_service)
    before = service.store.snapshot(sid)
    def forbidden(*args, **kwargs):
        raise AssertionError('Pure capability repair must not call a recommender or contract reviewer')
    service.setup_recommender = service.recovery_reviewer = forbidden
    assert facade.snapshot(aid)['next_schedule_at'] == ''
    assert facade.supervisor.scheduler.advance(aid)  # No manual request or 900-second clock advance.
    after = service.store.snapshot(sid)
    assert after['task']['state'] == 'queued'
    assert after['run']['id'] == before['run']['id']
    assert after['run']['checkpoint'] == before['run']['checkpoint']
    assert after['context'] == before['context']
    assert after['plan'] == before['plan'] and after['acceptance'] == before['acceptance']
    assert after['run']['active_seconds'] == 20
    assert after['run']['tool_extension_pending']['additions'] == ['shell']
    history = facade.snapshot(aid)['work_packages'][0]['recovery']['history']
    assert history[0]['kind'] == 'extend_tools' and history[0]['report']['reset'] == []
    events = service.events(sid, limit=1000)
    assert any(e['type'] == 'task.tools.extended' for e in events)
    assert not any(e['type'] == 'task.restarted' for e in events)


def test_capability_wake_is_durable_and_deduplicated(agent_service):
    service, facade, aid, work, sid = capability_blocked(agent_service)
    with service.store.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['next_schedule_at'] = '2099-01-01T00:00:00+00:00'
        facade.store.put(db, 'agents', agent)
    service.store.update(sid, lambda *args: None)
    assert facade.snapshot(aid)['next_schedule_at'].startswith('2099')
    service.store.update(sid, lambda state, emit, db: state['run'].update(attempt_id='next-attempt'))
    assert facade.snapshot(aid)['next_schedule_at'] == ''


@pytest.mark.parametrize('guard', ['human', 'disabled', 'input', 'taken_over'])
def test_capability_wake_respects_control_and_policy(agent_service, guard):
    service, facade, aid, work, sid = capability_blocked(agent_service)
    with service.store.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['next_schedule_at'] = '2099-01-01T00:00:00+00:00'
        if guard == 'disabled':
            agent['role']['scheduling_policy']['enabled'] = False
        if guard == 'taken_over':
            db.execute('UPDATE agent_sessions SET taken_over=1 WHERE session_id=?', (sid,))
        facade.store.put(db, 'agents', agent)
    def update(state, emit, db):
        state['run']['attempt_id'] = 'new-attempt'
        if guard == 'human':
            state['control'] = dict(command_id='human-pause', kind='paused')
        if guard == 'input':
            state['input_request'] = dict(state='pending')
    service.store.update(sid, update)
    assert facade.snapshot(aid)['next_schedule_at'].startswith('2099')


def test_worker_exit_race_rechecks_promptly_instead_of_waiting_full_interval(agent_service):
    service, facade, aid, work, sid = capability_blocked(agent_service)
    service.active[sid] = object()
    try:
        facade.supervisor.scheduler.advance(aid)
        from datetime import datetime
        next_check = datetime.fromisoformat(facade.snapshot(aid)['next_schedule_at'])
        assert (next_check - facade.store.clock()).total_seconds() <= 1
        assert service.store.snapshot(sid)['task']['state'] == 'paused'
    finally:
        service.active.pop(sid)


def test_stale_prepared_extension_cannot_replace_user_configuration(agent_service):
    service, facade, aid, work, sid = capability_blocked(agent_service)
    original_submit = service.submit
    def interrupted(sid, command):
        service.store.update(sid, lambda state, emit, db: state['task']['task_spec']['workflow'].update(mode='off'))
        return original_submit(sid, command)
    service.submit = interrupted
    before = deepcopy(service.store.snapshot(sid)['task']['task_spec']['tools'])
    facade.supervisor.scheduler.advance(aid)
    assert facade.snapshot(aid)['scheduler']['state'] == 'intervention'
    assert service.store.snapshot(sid)['task']['task_spec']['tools'] == before
    assert service.store.snapshot(sid)['task']['state'] == 'paused'


def extension_provider(state):
    import json

    from loom.core import ok
    from loom.llm.api import LlmResponse, LlmToolCall
    from tests.tasks.test_acceptance import Judge

    read = dict(id='read', description='Read fixture source', verifier='tool_call', scope=['notes.txt'],
                check=dict(tool_id='read_file'), required_tools=['read_file'])
    class Provider:
        model = 'tool-extension-fixture'
        verification_provider = Judge([read])

        async def chat(self, messages, tools=None, **kwargs):
            observed = {m.name for m in messages if m.role == 'tool'}
            available = {t['function']['name'] for t in tools or []}
            if 'shell_execute' in available:
                assert any('retained across tool extension' in (m.content or '') for m in messages)
                if not getattr(self, 'validated', False):
                    self.validated = True
                    call = LlmToolCall('validate-source', 'shell_execute', '{"command":"test -s notes.txt","cwd":"."}')
                else:
                    call = LlmToolCall('finish', 'finish', '{"report":"Source read and validated"}')
            elif 'read_file' not in observed:
                call = LlmToolCall('read-source', 'read_file', '{"path":"notes.txt"}')
            elif 'revise_acceptance_plan' not in observed:
                call = LlmToolCall('require-shell', 'revise_acceptance_plan', json.dumps(dict(base_revision=1,
                    reason='Validation discovered during inspection', criteria=[read,
                        dict(id='validate', description='Validate source', verifier='semantic', scope=[], check={},
                             required_tools=['shell_execute'])])))
            else:
                call = LlmToolCall('finish', 'finish', '{"report":"Source read and validated"}')
            return ok(LlmResponse('', (call,)))
    return Provider()


@pytest.mark.parametrize('restart_before_resume', [False, True])
def test_real_worker_extends_tools_and_completes_with_original_read_evidence(tmp_path, restart_before_resume):
    from loom.service.controller import LoomService
    from tests.autonomy.helpers import evaluation_provider, goals, role, single_package
    from tests.autonomy.test_execution_flow import until

    (tmp_path / 'notes.txt').write_text('Session fixture evidence: retained across tool extension')
    service = LoomService(tmp_path / 'data', autonomy_enabled=True, provider_factory=extension_provider,
                          agent_planner=single_package, evaluation_provider_factory=evaluation_provider)
    try:
        service.setup_recommender = lambda *args, **kwargs: dict(task_type='general',
            tool_collections=['filesystem', 'task_control'], plugins={'workflow': 'dynamic'}, plan_mode='off')
        facade = service.background.autonomy
        agent_role = role(tmp_path)
        agent_role['scheduling_policy'] = {'tick_seconds': 900}
        aid = facade.store.create('create', agent_role)['id']
        facade.store.add_goal(aid, 'goal', goals()[0])
        facade.control(aid, dict(command_id='enable', type='enable', expected_revision=1))
        if restart_before_resume:
            submit = service.submit
            def hold_resume(sid, command):
                if command['type'] == 'resume':
                    service.capacity = 0  # Commit the extension, but leave its worker unstarted.
                return submit(sid, command)
            service.submit = hold_resume
        service.start()
        if restart_before_resume:
            until(lambda: service.store.list_sessions(), lambda rows: bool(rows)
                  and rows[0]['task']['state'] == 'queued' and bool((rows[0].get('run') or {}).get('tool_extension_pending')))
            service.close()
            service = LoomService(tmp_path / 'data', autonomy_enabled=True, provider_factory=extension_provider,
                                  agent_planner=single_package, evaluation_provider_factory=evaluation_provider).start()
            facade = service.background.autonomy
        completed = until(lambda: facade.snapshot(aid), lambda a: any(w['state'] == 'succeeded' for w in a['work_packages']))
        work = completed['work_packages'][0]
        assert len(work['run_ids']) == 1
        assert work['recovery']['attempts'] == 1
        sid = work['session_id']
        events = service.events(sid, limit=1000)
        assert sum(e['type'] == 'tool.completed' and e['payload'].get('tool_id') == 'read_file' for e in events) == 1
        assert sum(e['type'] == 'acceptance.plan.accepted' for e in events) == 1
        assert sum(e['type'] == 'acceptance.plan.revised' for e in events) == 1
        assert any(e['type'] == 'task.tools.extended' for e in events)
        assert not any(e['type'] == 'task.restarted' for e in events)
        assert service.store.snapshot(sid)['acceptance']['state'] == 'passed'
    finally:
        service.close()
