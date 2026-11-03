from copy import deepcopy

import pytest

from loom.autonomy.configuration import activate
from loom.autonomy.sources import refresh
from loom.service.contracts import ServiceError
from tests.autonomy.test_dispatch import prepare
from tests.autonomy.test_learning import terminal


def save(facade, aid, section, changes, cid='settings', revision=None):
    return facade.dispatch('POST', [aid, 'configuration', section], {}, dict(
        command_id=cid, expected_revision=revision or facade.snapshot(aid)['revision'], changes=changes,
    ))


@pytest.mark.parametrize(('section', 'changes'), [
    ('budget', {'workspace': '/tmp'}),
    ('identity', {'long_term_objectives': []}),
    ('models', {'role_text': 'overwritten'}),
    ('budget', {'budget': {'objective_tokens': 100000000}}),
    ('scheduling', {'scheduling_policy': {'auto_expand': True}}),
    ('unknown', {'name': 'new'}),
    ('budget', {}),
    ('budget', {'budget': {'daily_tokens': -1}}),
])
def test_settings_reject_out_of_scope_or_invalid_values(agent_service, section, changes):
    _, facade, aid, _ = agent_service
    before = facade.snapshot(aid)
    with pytest.raises(ServiceError):
        save(facade, aid, section, changes)
    after = facade.snapshot(aid)
    assert after['role'] == before['role']
    assert after['revision'] == before['revision']
    assert not after['configuration']['pending']


def test_settings_are_idempotent_revision_checked_and_merged_by_section(agent_service):
    _, facade, aid, _ = agent_service
    before = facade.snapshot(aid)
    changes = {'budget': {'daily_tokens': 2000000}}
    receipt = save(facade, aid, 'budget', changes, revision=before['revision'])
    assert save(facade, aid, 'budget', changes, revision=before['revision']) == receipt
    with pytest.raises(ServiceError, match='Revision'):
        save(facade, aid, 'models', {'model': 'new-model'}, cid='stale', revision=before['revision'])
    save(facade, aid, 'models', {'model': 'new-model'}, cid='model')
    current = facade.snapshot(aid)
    assert current['role'] == before['role']
    assert current['configuration']['role']['budget']['daily_tokens'] == 2000000
    assert current['configuration']['role']['model'] == 'new-model'
    assert current['role_revision'] == before['role_revision']
    assert current['generation'] == before['generation']
    assert current['configuration']['role']['workspace'] == before['role']['workspace']
    assert current['configuration']['role']['long_term_objectives'] == before['role']['long_term_objectives']


def test_pending_work_uses_new_settings_without_revising_delivery_contract(agent_service):
    service, facade, aid, path = agent_service
    (path / 'src').mkdir()
    (path / 'tests').mkdir()
    refresh(facade.store, aid, force=True)
    work = prepare(facade, aid)
    original = deepcopy(facade.snapshot(aid)['role'])
    save(facade, aid, 'models', {'model': 'new-model'})
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    current = facade.snapshot(aid)
    dispatched = current['work_packages'][0]
    assert dispatched['state'] == 'dispatched'
    assert dispatched['role_revision'] == current['role_revision'] == work['role_revision'] + 1
    assert current['role']['model'] == 'new-model'
    assert not current['configuration']['pending']
    task = service.store.snapshot(dispatched['session_id'])['task']
    assert task['model'] == 'new-model'
    assert dispatched['acceptance_criteria'] == work['acceptance_criteria']
    assert current['role']['long_term_objectives'] == original['long_term_objectives']
    with facade.store.sessions.read() as db:
        assert facade.store.rows(db, 'agent_sources', aid)[0]['role_revision'] == current['role_revision']
        assert facade.store.get(db, 'agent_roles', f"{aid}:{work['role_revision']}")['role'] == original


@pytest.mark.parametrize('state', ['dispatched', 'running', 'blocked', 'failed'])
def test_already_dispatched_work_and_recovery_keep_original_settings(agent_service, state):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    before = facade.snapshot(aid)
    work = before['work_packages'][0]
    task = deepcopy(service.store.snapshot(work['session_id'])['task'])
    with facade.store.sessions.transaction() as db:
        work['state'] = state
        facade.store.put(db, 'agent_work_packages', work)
    save(facade, aid, 'budget', {'budget': {'daily_tokens': 2000000}})
    with facade.store.sessions.transaction() as db:
        assert not activate(facade.store, db, aid)
    current = facade.snapshot(aid)
    assert current['role'] == before['role']
    assert current['configuration']['pending']
    assert current['work_packages'][0]['role_revision'] == work['role_revision']
    assert service.store.snapshot(work['session_id'])['task'] == task
    # Only after the original task terminates can settings activate for new work.
    terminal(service, work['session_id'])
    with facade.store.sessions.transaction() as db:
        assert activate(facade.store, db, aid)
    assert facade.snapshot(aid)['role']['budget']['daily_tokens'] == 2000000
    task['state'] = 'idle'
    assert service.store.snapshot(work['session_id'])['task'] == task


def test_started_learning_holds_activation_but_unstarted_learning_does_not(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    work = facade.snapshot(aid)['work_packages'][0]
    terminal(service, work['session_id'])
    cycle = facade.snapshot(aid)['learning'][0]
    with facade.store.sessions.transaction() as db:
        cycle.update(evaluation_id='existing-evaluation', state='deferred')
        facade.store.put(db, 'agent_learning_cycles', cycle)
    save(facade, aid, 'models', {'evaluation_model': 'new-judge'})
    with facade.store.sessions.transaction() as db:
        assert not activate(facade.store, db, aid)
        cycle.update(state='completed')
        facade.store.put(db, 'agent_learning_cycles', cycle)
        assert activate(facade.store, db, aid)
    assert facade.snapshot(aid)['role']['evaluation_model'] == 'new-judge'


def test_legacy_role_revision_cannot_change_workspace(agent_service, tmp_path):
    _, facade, aid, _ = agent_service
    agent = facade.snapshot(aid)
    role = deepcopy(agent['role'])
    role.pop('schema_version')
    other = tmp_path / 'other'
    other.mkdir()
    role.update(workspace=str(other), working_directory=str(other))
    with pytest.raises(ServiceError, match='workspace is fixed'):
        facade.store.revise_role(aid, 'move', agent['revision'], role)


def test_reserved_request_holds_old_daily_limit_until_settled(agent_service):
    from loom.autonomy.budget import BudgetLedger

    _, facade, aid, _ = agent_service
    ledger = BudgetLedger(facade.store.sessions.path)
    ledger.reserve('in-flight', {'agent_id': aid}, 100, 'execution')
    save(facade, aid, 'budget', {'budget': {'daily_tokens': 2000000}})
    with facade.store.sessions.transaction() as db:
        assert not activate(facade.store, db, aid)
    assert ledger.daily_summary(aid)['limit'] == 1000000
    ledger.settle('in-flight', 80)
    with facade.store.sessions.transaction() as db:
        assert activate(facade.store, db, aid)
    assert ledger.daily_summary(aid)['limit'] == 2000000
    assert ledger.daily_summary(aid)['used'] == 80


def test_settings_do_not_supersede_an_active_stage_or_its_acceptance(agent_service):
    from tests.autonomy.test_stage_progression import setup_plan

    _, facade, aid, _ = agent_service
    original = setup_plan(facade, aid)
    stage = original['stages'][0]
    save(facade, aid, 'identity', {'methods': ['Inspect current evidence before changes']})
    facade.supervisor.stages.advance(aid)
    current = facade.store.snapshot(aid)
    assert len(current['stages']) == len(original['stages'])
    same = next(row for row in current['stages'] if row['id'] == stage['id'])
    assert same['state'] == stage['state']
    assert same['goal_ids'] == stage['goal_ids']
    assert same['acceptance_criteria'] == stage['acceptance_criteria']
    assert current['role']['long_term_objectives'] == original['role']['long_term_objectives']


def test_disabling_timer_activates_without_scheduling_a_task(agent_service):
    _, facade, aid, _ = agent_service
    save(facade, aid, 'scheduling', {'scheduling_policy': {'enabled': False}})
    assert not facade.supervisor.scheduler.advance(aid)
    current = facade.snapshot(aid)
    assert not current['configuration']['pending']
    assert current['role']['scheduling_policy']['enabled'] is False
    assert not current['sessions']


def test_activation_during_dispatch_recommendation_cannot_mix_role_versions(agent_service, monkeypatch):
    from loom.autonomy import dispatch

    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    delegate = dispatch.bind_for_dispatch

    def configure_before_binding(*args):
        binding = delegate(*args)
        save(facade, aid, 'models', {'model': 'new-model'})
        with facade.store.sessions.transaction() as db:
            assert activate(facade.store, db, aid)
        return binding

    monkeypatch.setattr(dispatch, 'bind_for_dispatch', configure_before_binding)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    assert not service.store.list_sessions()
    monkeypatch.setattr(dispatch, 'bind_for_dispatch', delegate)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    dispatched = facade.snapshot(aid)['work_packages'][0]
    assert service.store.snapshot(dispatched['session_id'])['task']['model'] == 'new-model'


def test_pending_settings_hold_unstarted_learning_until_planning_finishes(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    terminal(service, facade.snapshot(aid)['work_packages'][0]['session_id'])
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['planner_lease'] = 'in-flight-planning'
        facade.store.put(db, 'agents', agent)
    save(facade, aid, 'models', {'evaluation_model': 'next-evaluation'})
    facade.supervisor.learning.advance(aid)
    current = facade.snapshot(aid)
    assert current['configuration']['pending']
    assert current['learning'][0]['state'] == 'pending'
    assert not current['learning'][0].get('evaluation_id')
