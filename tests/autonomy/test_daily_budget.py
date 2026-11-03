"""One shared local-day allowance, with immutable historical accounting."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.store import AgentStore
from loom.service.contracts import ServiceError
from loom.service.store import SessionStore
from tests.autonomy.helpers import Clock, goals, role


def daily_agent(tmp_path, *, limit=1000, zone='Asia/Shanghai'):
    clock = Clock()
    clock.value = datetime(2026, 11, 2, 15, 59, 59, tzinfo=UTC)
    store = AgentStore(SessionStore(tmp_path / 'daily-data'), clock=clock)
    config = role(tmp_path)
    config.update(timezone=zone, budget=dict(daily_tokens=limit, goal_tokens=1, work_tokens=1, learning_tokens=1, max_calls=1))
    config['long_term_objectives'][0]['stage_planning'] = dict(mode='autonomous', objective_tokens=1)
    aid = store.create('daily-agent', config)['id']
    gid = store.add_goal(aid, 'daily-goal', goals()[0])['id']
    store.command(aid, 'daily-enable', 'enable', 1)
    binding = dict(agent_id=aid, goal_id=gid, work_package_id='work', run_id='run', objective_id='quality')
    return store, clock, BudgetLedger(store.sessions.path, clock=clock), binding


def test_all_categories_share_daily_cap_and_ignore_legacy_sub_limits(tmp_path):
    store, clock, ledger, binding = daily_agent(tmp_path)
    for category in ('execution', 'acceptance', 'planning', 'stage_planning', 'learning', 'method_learning'):
        ledger.reserve(category, binding, 150, category)
        ledger.settle(category, 100)
    summary = ledger.daily_summary(binding['agent_id'])
    assert summary['used'] == 600 and summary['remaining'] == 400 and summary['calls'] == 6
    with pytest.raises(ServiceError) as failure:
        ledger.reserve('over-daily', binding, 401, 'execution')
    assert [s['scope'] for s in failure.value.budget_block['scopes']] == ['daily']
    assert len(ledger.summary(binding['agent_id'])) == 6
    objective = ledger.objective_summary(binding['agent_id'])['objectives'][0]
    assert objective['limit'] is None and objective['actual_tokens'] == 600


@pytest.mark.parametrize('zone,before,expected_day', [
    ('Asia/Shanghai', datetime(2026, 11, 2, 15, 59, 59, tzinfo=UTC), '2026-11-03'),
    ('America/New_York', datetime(2026, 3, 8, 4, 59, 59, tzinfo=UTC), '2026-03-08'),
])
def test_local_midnight_resets_allowance_preserves_unknown_history_and_late_settlement(tmp_path, zone, before, expected_day):
    store, clock, ledger, binding = daily_agent(tmp_path, zone=zone)
    clock.value = before
    ledger.reserve('yesterday-unknown', binding, 1000, 'execution')
    ledger.settle('yesterday-unknown', None)
    with pytest.raises(ServiceError):
        ledger.reserve('yesterday-denied', binding, 1, 'learning')
    clock.advance(2)
    summary = ledger.daily_summary(binding['agent_id'])
    assert summary['window'] == expected_day and summary['used'] == 0 and summary['remaining'] == 1000
    if zone == 'America/New_York':
        assert summary['resets_at'] == '2026-03-09T04:00:00+00:00'
    ledger.reserve('today', binding, 1000, 'learning')
    ledger.settle('yesterday-unknown', 800)
    assert ledger.daily_summary(binding['agent_id'])['used'] == 1000
    assert sum(row['tokens'] for row in ledger.summary(binding['agent_id'])) == 1800


def test_concurrent_calls_cannot_overreserve_the_shared_daily_cap(tmp_path):
    store, clock, ledger, binding = daily_agent(tmp_path, limit=100)
    def reserve(identifier):
        try:
            ledger.reserve(identifier, binding, 60, 'execution')
            return True
        except ServiceError as error:
            assert error.code == 'AGENT_BUDGET'
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ['one', 'two'])) == [False, True]
    assert ledger.daily_summary(binding['agent_id'])['used'] == 60


def test_existing_agent_sessions_migrate_to_daily_mode_idempotently(tmp_path):
    store, clock, ledger, binding = daily_agent(tmp_path)
    state = store.sessions.create('existing-session', dict(objective='Existing task', workspace=str(tmp_path)))
    sid = state['session_id']
    store.sessions.update(sid, lambda s, emit, db: s.update(autonomy=binding))
    with store.sessions.transaction() as db:
        db.execute('DELETE FROM autonomy_migrations WHERE version=6')
    AgentStore(store.sessions, clock=clock)
    migrated = store.sessions.snapshot(sid)
    assert migrated['task']['limits']['max_tokens'] is None
    cursor = migrated['event_cursor']
    AgentStore(store.sessions, clock=clock)
    assert store.sessions.snapshot(sid)['event_cursor'] == cursor


def test_agent_setup_recommendations_share_daily_admission_before_network_send(agent_service):
    from loom.service.session_setup import recommend_setup
    from tests.service.test_session_setup import Recommender

    service, facade, aid, _ = agent_service
    gid = facade.store.add_goal(aid, 'setup-goal', goals()[0])['id']
    class MeteredRecommender(Recommender):
        async def chat(self, messages, tools=None, cancellation=None):
            return await super().chat(messages, tools)
    provider = MeteredRecommender()
    binding = dict(agent_id=aid, goal_id=gid, work_package_id='setup-work')
    recommend_setup(service, 'Inspect sources', scope='dispatch', provider_factory=lambda model: provider, budget_binding=binding)
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['role']['budget']['daily_tokens'] = 30
        facade.store.put(db, 'agents', agent)
    with pytest.raises(ServiceError) as denied:
        recommend_setup(service, 'Inspect sources', scope='repair', provider_factory=lambda model: provider, budget_binding=binding)
    assert denied.value.code == 'AGENT_BUDGET'
    assert denied.value.budget_block['scopes'][0]['scope'] == 'daily'
    assert len(provider.calls) == 1
    assert BudgetLedger(service.store.path).daily_summary(aid)['used'] == 30
