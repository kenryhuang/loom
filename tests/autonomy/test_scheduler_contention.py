import sqlite3

import pytest

from loom.autonomy.budget import BudgetLedger


@pytest.mark.parametrize('mode', ['before-call', 'leased', 'released-after-call'])
def test_storage_wait_retries_only_before_model_work_starts(agent_service, monkeypatch, mode):
    _, facade, aid, _ = agent_service
    scheduler, store = facade.supervisor.scheduler, facade.store
    calls = []

    def cycle(identifier):
        calls.append(identifier)
        if len(calls) == 1:
            if mode == 'leased':
                with store.sessions.transaction() as db:
                    agent = store.get(db, 'agents', aid)
                    agent['planner_lease'] = 'paid-planning'
                    store.put(db, 'agent_runs', dict(id='paid-planning', agent_id=aid, state='planning'))
                    store.put(db, 'agents', agent)
            elif mode == 'released-after-call':
                BudgetLedger(store.sessions.path).reserve('paid-planning', dict(agent_id=aid), 1, 'planning')
            raise sqlite3.OperationalError('database is locked')
        scheduler.finish(aid, 'waiting', 'Storage recovered')

    monkeypatch.setattr(scheduler, '_cycle', cycle)
    scheduler.advance(aid, manual=True)
    state = store.snapshot(aid)
    may_have_called = mode != 'before-call'
    assert state['scheduler']['state'] == ('intervention' if may_have_called else 'waiting')
    assert bool(state.get('error')) == may_have_called
    with store.sessions.transaction() as db:
        agent = store.get(db, 'agents', aid)
        agent['next_schedule_at'] = store.now()
        store.put(db, 'agents', agent)
    scheduler.advance(aid)
    assert len(calls) == (1 if may_have_called else 2)
