"""Regressions for budget pauses after repeated failure/recovery intervals."""
import json

import pytest

from loom.autonomy import sources
from loom.autonomy.stages import planning_state, role_signature
from tests.autonomy.test_queue_scheduling import paused_work
from tests.autonomy.test_stage_contracts import configure


def staged_pause(agent_service):
    service, facade, aid, root = agent_service
    configure(facade, aid)
    _, _, _, work, sid = paused_work(agent_service, 'localized budget pause', stop_code='ACTIVE_TIME_BUDGET_EXCEEDED')
    (root / 'src').mkdir()
    (root / 'tests').mkdir()
    (root / 'src' / 'file.py').write_text('before')
    sources.refresh(facade.store, aid, force=True)
    with service.store.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        goal = facade.store.get(db, 'agent_goals', work['goal_id'])
        goal.update(stage_id='stage', required_source_ids=['code'])
        facade.store.put(db, 'agent_goals', goal)
        stage = dict(id='stage', agent_id=aid, objective_id='quality', revision=1, state='active', title='Budget recovery', expected_outcome='Finish',
                     scope=[], non_goals=[], acceptance=[], stop_conditions=[], goal_ids=[goal['id']], role_signature=role_signature(agent['role']))
        facade.store.put(db, 'agent_stages', stage)
        planning = planning_state(facade.store, db, agent, agent['role']['long_term_objectives'][0])
        planning.update(state='active', stage_id=stage['id'])
        facade.store.put(db, 'agent_objective_planning', planning)
    return service, facade, aid, root, sid


def execution_event(service, facade, sid):
    facade.store.clock.advance(10)
    # A durable host event between two source refreshes. The work package has
    # returned to the same blocked state by the time the next refresh runs.
    with service.store.transaction() as db:
        state = service.store._load(db, sid)
        e = service.store.append(db, state, 'run.started', {})
        e['at'] = facade.store.now()
        db.execute('UPDATE events SET body=? WHERE session_id=? AND seq=?', (json.dumps(e), sid, e['seq']))
        service.store._save(db, state)
    facade.store.clock.advance(10)


@pytest.mark.parametrize('legacy', [False, True])
def test_self_changes_between_retries_do_not_block_time_recovery(agent_service, monkeypatch, legacy):
    service, facade, aid, root, sid = staged_pause(agent_service)
    execution_event(service, facade, sid)
    (root / 'src' / 'file.py').write_text('after own execution')
    if legacy:
        with monkeypatch.context() as patch:
            patch.setattr(sources, 'executed_between', lambda *a: False)
            sources.refresh(facade.store, aid, force=True)
        facade.supervisor.stages.reconcile(aid)
        assert facade.snapshot(aid)['objective_planning'][0]['state'] == 'blocked'
    else:
        sources.refresh(facade.store, aid, force=True)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'recovered', json.dumps(snap['scheduler'], ensure_ascii=False)
    assert snap['objective_planning'][0]['state'] == 'active'
    assert not snap['goals'][0].get('source_changed')
    assert service.snapshot(sid)['task']['state'] == 'queued'
    with service.store.read() as db:
        events = facade.store.rows(db, 'agent_external_events', aid)
        assert events[-1]['state'] == 'suppressed'
        count = db.execute("SELECT count(*) FROM agent_events WHERE json_extract(body,'$.type')='source.attribution.corrected'").fetchone()[0]
        assert count == int(legacy)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)['work_packages'][0]['recovery']['attempts'] == 1


def test_external_source_change_without_execution_still_blocks_resume(agent_service):
    service, facade, aid, root, sid = staged_pause(agent_service)
    facade.store.clock.advance(20)
    (root / 'src' / 'file.py').write_text('external change')
    sources.refresh(facade.store, aid, force=True)
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'intervention'
    assert snap['goals'][0]['source_changed']
    assert service.snapshot(sid)['task']['state'] == 'paused'
    assert not snap['work_packages'][0].get('recovery', {}).get('attempts')


@pytest.mark.parametrize('goal_state', ['active', 'blocked'])
def test_explicit_external_event_during_execution_is_not_reclassified(agent_service, goal_state):
    from loom.autonomy.events import ingest

    service, facade, aid, _, sid = staged_pause(agent_service)
    execution_event(service, facade, sid)
    with service.store.transaction() as db:
        goal = facade.store.rows(db, 'agent_goals', aid)[0]
        goal['state'] = goal_state
        facade.store.put(db, 'agent_goals', goal)
    event = ingest(facade.store, aid, dict(command_id='external', source_id='code', event_id='external',
        object_id='src', version=50, occurred_at=facade.store.now(), cursor='50', type='changed', summary='External edit'))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.snapshot(aid)
    assert snap['scheduler']['state'] == 'intervention'
    assert snap['goals'][0]['source_changed'] == event['id']
    assert service.snapshot(sid)['task']['state'] == 'paused'
    assert not snap['work_packages'][0].get('recovery', {}).get('attempts')
