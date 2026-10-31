import pytest

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.contracts import check_dependencies, role_contract
from loom.autonomy.store import AgentStore
from loom.service.contracts import ServiceError
from loom.service.store import SessionStore
from tests.autonomy.helpers import Clock, goals, role


def test_role_scope_and_dependencies(tmp_path):
    assert role_contract(role(tmp_path))["workspace"] == str(tmp_path)
    with pytest.raises(ServiceError, match="outside workspace"):
        role_contract({**role(tmp_path), "source_routes": [{"id": "bad", "topic": "bad", "paths": ["../"]}]})
    with pytest.raises(ServiceError, match="cycle"):
        check_dependencies([{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}])


def test_store_replay_revisions_and_restart(tmp_path):
    store = AgentStore(SessionStore(tmp_path / "data"))
    first = store.create("create", role(tmp_path))
    assert store.create("create", role(tmp_path)) == first
    with pytest.raises(ServiceError, match="different input"):
        store.create("create", {**role(tmp_path), "name": "other"})
    goal = store.add_goal(first["id"], "goal", goals()[0])
    enabled = store.command(first["id"], "enable", "enable", 1)
    restored = AgentStore(SessionStore(tmp_path / "data"))
    assert restored.snapshot(first["id"])["goals"] == [goal]
    assert restored.snapshot(first["id"])["state"] == "enabled"
    with pytest.raises(ServiceError, match="Revision conflict"):
        restored.command(first["id"], "pause", "pause", 1)
    assert enabled["revision"] == 2


def test_budget_reserves_unknown_and_keeps_goal_across_days(tmp_path):
    clock = Clock()
    store = AgentStore(SessionStore(tmp_path / "data"), clock=clock)
    agent = store.create("create", {**role(tmp_path), "budget": {"daily_tokens": 100, "goal_tokens": 150}})
    store.command(agent["id"], "enable", "enable", 1)
    ledger = BudgetLedger(store.sessions.path, clock=clock)
    binding = dict(agent_id=agent["id"], goal_id="goal")
    ledger.reserve("one", binding, 80, "execution")
    ledger.settle("one", None)
    with pytest.raises(ServiceError, match="budget exhausted"):
        ledger.reserve("two", binding, 30, "execution")
    clock.advance(86400)
    ledger.reserve("two", binding, 30, "execution")
    ledger.settle("two", 20)
    ledger.settle("two", 20)
    with pytest.raises(ServiceError, match="budget exhausted"):
        ledger.reserve("three", binding, 60, "execution")
    assert sum(r["tokens"] for r in ledger.summary(agent["id"])) == 100


@pytest.mark.parametrize('daily,expected', [(1000000, 100000), (100000000, 10000000), (1234567, 123456), (9, 1)])
def test_work_budget_defaults_to_tenth_of_daily_budget(tmp_path, daily, expected):
    configured = role_contract({**role(tmp_path), 'budget': {'daily_tokens': daily}})['budget']
    assert configured['work_tokens'] == expected
    assert configured['max_calls'] == 2000
    assert configured['goal_tokens'] == 100000000
    explicit = role_contract({**role(tmp_path), 'budget': {'daily_tokens': daily, 'work_tokens': 250000, 'max_calls': 500}})['budget']
    assert explicit['work_tokens'] == 250000
    assert explicit['max_calls'] == 500


@pytest.mark.parametrize('value', [True, 0, -1, '1000000', None])
def test_work_budget_derivation_does_not_accept_invalid_daily_limit(tmp_path, value):
    with pytest.raises(ServiceError, match='daily_tokens'):
        role_contract({**role(tmp_path), 'budget': {'daily_tokens': value}})


@pytest.mark.parametrize('scope,category', [('daily', 'execution'), ('goal', 'execution'), ('work', 'execution'), ('learning', 'learning')])
def test_budget_error_identifies_scope_and_reservation_arithmetic(tmp_path, scope, category):
    clock = Clock()
    store = AgentStore(SessionStore(tmp_path / 'data'), clock=clock)
    policy = dict(daily_tokens=1000, goal_tokens=1000, work_tokens=1000, learning_tokens=1000)
    policy[f'{scope}_tokens'] = 100
    agent = store.create('create', {**role(tmp_path), 'budget': policy})
    store.command(agent['id'], 'enable', 'enable', 1)
    ledger = BudgetLedger(store.sessions.path, clock=clock)
    binding = dict(agent_id=agent['id'], goal_id='goal', work_package_id='work')
    ledger.reserve('settled', binding, 50, category)
    ledger.settle('settled', 30)
    ledger.reserve('unknown', binding, 40, category)
    ledger.settle('unknown', None)
    ledger.reserve('pending', binding, 10, category)
    with pytest.raises(ServiceError) as caught:
        ledger.reserve('rejected', binding, 25, category)
    message = str(caught.value)
    assert caught.value.code == 'AGENT_BUDGET'
    for detail in [f'Agent {scope} token budget exhausted', 'limit=100', 'used=80', 'actual=30',
                   'reserved_or_unknown=50', 'requested=25', 'projected=105', 'remaining=20', 'exceeded_by=5',
                   f'category={category}', f'increase {scope}_tokens']:
        assert detail in message
    if scope in {'goal', 'work'}:
        assert 'does not reset daily' in message
        assert ('goal_id=goal' if scope == 'goal' else 'work_package_id=work') in message
    else:
        assert 'window=2026-10-30' in message and 'timezone=' in message
    assert sum(r['calls'] for r in ledger.summary(agent['id'])) == 3


def test_budget_reports_all_exceeded_limits_and_daily_calls(tmp_path):
    store = AgentStore(SessionStore(tmp_path / 'data'))
    agent = store.create('create', {**role(tmp_path), 'budget': dict(daily_tokens=10, goal_tokens=10, work_tokens=10, max_calls=1)})
    store.command(agent['id'], 'enable', 'enable', 1)
    ledger = BudgetLedger(store.sessions.path)
    binding = dict(agent_id=agent['id'], goal_id='g', work_package_id='w')
    ledger.reserve('one', binding, 10, 'execution')  # Exact limit is allowed.
    with pytest.raises(ServiceError) as caught:
        ledger.reserve('two', binding, 1, 'execution')
    message = str(caught.value)
    for scope in ('daily', 'goal', 'work'):
        assert f'Agent {scope} token budget exhausted' in message
    assert 'Agent daily call budget exhausted: limit=1, used=1, requested=1, projected=2, remaining=0, exceeded_by=1 calls' in message
    assert 'increase max_calls' in message
