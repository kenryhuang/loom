import json

import pytest

from loom.service.failures import failure_details
from loom.tasks.recovery import assess
from tests.autonomy.test_queue_scheduling import paused_work


def failed_work(agent_service, *, legacy=False, failure=None):
    service, facade, aid, work, sid = paused_work(agent_service, 'arbitrary text')
    failure = failure if failure is not None else dict(code='LLM_FAILED' if legacy else 'LLM_TRANSPORT_ERROR',
        message='arbitrary text', retryable=True, cause=dict(name='URLError'))

    def fail(state, emit, db):
        state['run'].update(state='failed', failure={k: v for k, v in failure.items() if not legacy or k in {'code', 'message'}})
        state['task']['state'] = 'failed'
        emit('llm.failed', dict(error=failure))
        emit('run.failed', state['run']['failure'])

    service.store.update(sid, fail)
    return service, facade, aid, work, sid


@pytest.mark.parametrize('legacy', [False, True])
def test_retryable_provider_failure_resumes_checkpoint_without_reassembling(agent_service, legacy):
    service, facade, aid, work, sid = failed_work(agent_service, legacy=legacy)
    before = service.store.snapshot(sid)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'recovered', snap['scheduler']
    resumed = service.store.snapshot(sid)
    assert resumed['task']['state'] == 'queued'
    assert resumed['task']['task_spec'] == before['task']['task_spec']
    assert resumed['run']['checkpoint'] == before['run']['checkpoint']
    assert resumed['run']['id'] == before['run']['id']
    assert not resumed.get('recovery_context')
    assert snap['work_packages'][0]['recovery']['attempts'] == 1
    with service.store.read() as db:
        event = json.loads(db.execute("SELECT body FROM agent_events WHERE agent_id=? AND "
            "json_extract(body,'$.type')='schedule.recovery.requested' ORDER BY seq DESC LIMIT 1", (aid,)).fetchone()[0])
    report = event['payload']['report']
    assert report['mode'] == 'resume'
    assert report['causes'][0]['code'] == 'PROVIDER_TRANSIENT'
    assert report['causes'][0]['details']['failure']['retryable'] is True


@pytest.mark.parametrize('newer', ['llm.requested', 'llm.completed', 'run.started'])
def test_legacy_error_evidence_cannot_cross_new_requests_or_attempts(agent_service, newer):
    service, facade, aid, work, sid = failed_work(agent_service, legacy=True)
    service.store.update(sid, lambda state, emit, db: emit(newer, {}))
    with service.store.read() as db:
        failure = failure_details(db, service.store._load(db, sid))
    assert 'retryable' not in failure
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)['scheduler']['state'] == 'intervention'
    assert service.store.snapshot(sid)['task']['state'] == 'failed'


@pytest.mark.parametrize('failure', [dict(code='LLM_FAILED', retryable=False), dict(code='LLM_TLS_ERROR', retryable=False),
                                   dict(code='TOOL_FAILED', retryable=True)])
def test_nonretryable_or_nonprovider_failures_do_not_auto_resume(agent_service, failure):
    service, facade, aid, work, sid = failed_work(agent_service, failure={**failure, 'message': 'SSL: UNEXPECTED_EOF_WHILE_READING'})
    assert assess(service.store.snapshot(sid)) is None
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)['scheduler']['state'] == 'intervention'
    assert service.store.snapshot(sid)['task']['state'] == 'failed'


@pytest.mark.parametrize('guard', ['control', 'input', 'checkpoint'])
def test_retryable_provider_error_keeps_recovery_safety_guards(agent_service, guard):
    service, facade, aid, work, sid = failed_work(agent_service)

    def hold(state, emit, db):
        if guard == 'control':
            state['control'] = dict(command_id='human', kind='paused')
        elif guard == 'input':
            state['input_request'] = dict(id='question', state='pending', question='Verify effects')
        else:
            state['run']['checkpoint'] = None
            state['context'] = None

    service.store.update(sid, hold)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'intervention'
    assert not snap['work_packages'][0].get('recovery', {}).get('attempts')


def test_transient_retries_stop_at_the_existing_attempt_limit(agent_service):
    service, facade, aid, work, sid = failed_work(agent_service)
    facade.supervisor.scheduler.advance(aid, manual=True)
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['role']['scheduling_policy']['max_recovery_attempts'] = 1
        facade.store.put(db, 'agents', agent)
    service.store.update(sid, lambda state, emit, db: state['task'].update(state='failed'))
    facade.supervisor.scheduler.advance(aid, manual=True)
    saved = facade.snapshot(aid)['work_packages'][0]['recovery']
    assert saved['attempts'] == 1
    assert saved['exhausted'] is True
