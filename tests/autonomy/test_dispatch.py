import pytest

from loom.service.contracts import ServiceError
from tests.autonomy.helpers import Faults, goals


def prepare(facade, aid):
    facade.store.add_goal(aid, "goal", goals()[0])
    facade.supervisor._plan(facade.snapshot(aid))
    return facade.snapshot(aid)["work_packages"][0]


def test_committed_dispatch_response_loss_does_not_duplicate_session(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.fault = Faults("after_dispatch")
    with pytest.raises(RuntimeError, match="Injected"):
        facade.supervisor.dispatcher.dispatch(aid, work["id"])
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    assert len(service.store.list_sessions()) == 1
    state = service.store.list_sessions()[0]
    assert state["autonomy"]["work_package_id"] == work["id"]
    assert state["messages"][0]["producer"] == "agent_planner"
    with service.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM commands WHERE id LIKE 'agent-dispatch:%'").fetchone()[0] == 1
        assert db.execute("SELECT state FROM agent_outbox WHERE id=?", (work["id"],)).fetchone()[0] == "delivered"


def test_pause_invalidates_pending_dispatch(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.control(aid, dict(command_id="pause", type="pause", expected_revision=2))
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    assert service.store.list_sessions() == []


def test_planner_cannot_commit_after_goal_cancel(agent_service):
    _, facade, aid, _ = agent_service
    goal = facade.store.add_goal(aid, "goal", goals()[0])
    delegate = facade.supervisor.planner.provider

    def cancel_during_plan(agent, current_goal, context):
        facade.goal_command(aid, goal["id"], dict(command_id="cancel", type="cancel", expected_revision=1))
        return delegate(agent, current_goal, context)

    facade.supervisor.planner.provider = cancel_during_plan
    facade.supervisor._plan(facade.snapshot(aid))
    assert facade.snapshot(aid)["work_packages"] == []
    with facade.store.sessions.transaction() as db:
        assert facade.store.rows(db, "agent_runs", aid)[0]["state"] == "superseded"


def test_takeover_is_revision_checked_and_scope_checked(agent_service):
    service, facade, aid, path = agent_service
    sid = service.create("manual", {"workspace": str(path)})["session_id"]
    facade.session_command(aid, sid, dict(command_id="bind", type="bind", expected_revision=2))
    facade.session_command(aid, sid, dict(command_id="take", type="takeover", expected_revision=3))
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    assert facade.snapshot(aid)["work_packages"][0]["session_id"] != sid
    with pytest.raises(ServiceError, match="Revision"):
        facade.session_command(aid, sid, dict(command_id="release", type="release", expected_revision=3))


@pytest.mark.parametrize('budget_only', [True, False])
def test_budget_only_revision_preserves_pending_work_contract(agent_service, budget_only):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    agent = facade.snapshot(aid)
    role = {k: v for k, v in agent['role'].items() if k != 'schema_version'}
    role['budget'] = {**role['budget'], 'work_tokens': 1000000, 'max_calls': 2000}
    role['learning_policy'] = {**role['learning_policy'], 'max_learning_tokens': 2000000}
    if not budget_only:
        role['methods'] = [*role['methods'], 'Changed execution method']
    updated = facade.store.revise_role(aid, 'budget-revision', agent['revision'], role)
    assert facade.store.revise_role(aid, 'budget-revision', agent['revision'], role) == updated
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    current = facade.snapshot(aid)['work_packages'][0]
    if budget_only:
        assert current['state'] == 'dispatched'
        assert current['role_revision'] == updated['role_revision']
        assert service.store.snapshot(current['session_id'])['task']['limits']['max_tokens'] == 1000000
    else:
        assert current['state'] == 'blocked'
        assert 'role_changed' in current['blocked_reason']
