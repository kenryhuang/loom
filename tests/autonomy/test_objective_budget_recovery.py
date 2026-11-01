from copy import deepcopy

import pytest

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.execution_budget import objective_limit
from loom.autonomy.stages import planning_state, role_signature
from loom.service.contracts import ServiceError
from tests.autonomy.test_queue_scheduling import paused_work


def stopped_objective(agent_service, *, legacy=True, reviewing=True):
    service, facade, aid, _ = agent_service
    agent = facade.store.snapshot(aid)
    role = deepcopy(agent["role"])
    role.pop("schema_version", None)
    role["long_term_objectives"][0]["stage_planning"] = dict(mode="autonomous", objective_tokens=100)
    role["budget"].update(work_tokens=10000, goal_tokens=10000, daily_tokens=10000)
    role["scheduling_policy"].update(max_objective_tokens=400)
    facade.store.revise_role(aid, "objective-policy", agent["revision"], role)
    _, _, _, work, sid = paused_work(agent_service)
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        goal = facade.store.get(db, "agent_goals", work["goal_id"], aid)
        goal["stage_id"] = "budget-stage"
        facade.store.put(db, "agent_goals", goal)
        stage = dict(id="budget-stage", agent_id=aid, objective_id="quality", revision=1,
                     state="active", title="Budget recovery", expected_outcome="Finish original task",
                     scope=[], non_goals=[], acceptance=[], stop_conditions=[],
                     goal_ids=[goal["id"]], role_signature=role_signature(agent["role"]))
        facade.store.put(db, "agent_stages", stage)
    ledger = BudgetLedger(service.store.path)
    binding = dict(agent_id=aid, goal_id=work["goal_id"], work_package_id=work["id"], run_id="r1")
    ledger.reserve("used", binding, 80, "execution")
    ledger.settle("used", 80)
    with pytest.raises(ServiceError) as error:
        ledger.reserve("denied", binding, 50, "execution")

    def fail(state, emit, db):
        state["run"].update(state="failed", reason=None, failure=dict(code="LLM_FAILED", message=str(error.value)))
        state["task"]["state"] = "failed"
        emit("run.failed", state["run"]["failure"])

    service.store.update(sid, fail)
    with service.store.transaction() as db:
        if legacy:
            db.execute("DELETE FROM agent_budget_blocks")
        if reviewing:
            stage.update(state="reviewing")
            facade.store.put(db, "agent_stages", stage)
        planning = planning_state(facade.store, db, agent, agent["role"]["long_term_objectives"][0])
        planning.update(stage_id=stage["id"], state="deferred" if reviewing else "active", reason=str(error.value))
        facade.store.put(db, "agent_objective_planning", planning)
    return service, facade, aid, work, sid, ledger, binding


@pytest.mark.parametrize("legacy", [True, False])
def test_budget_stopped_goal_restores_stage_expands_objective_and_resumes_once(agent_service, legacy):
    service, facade, aid, work, sid, ledger, binding = stopped_objective(agent_service, legacy=legacy)
    facade.supervisor.scheduler.advance(aid, manual=True)
    result = facade.snapshot(aid)
    assert result["scheduler"]["state"] == "recovered", result["scheduler"]
    assert result["objective_budgets"]["quality"]["tokens"] == 200
    assert len(result["objective_budgets"]["quality"]["grants"]) == 1
    assert result["role"]["long_term_objectives"][0]["stage_planning"]["objective_tokens"] == 100
    assert result["role"]["budget"]["goal_tokens"] == 10000
    assert result["objective_budget"]["objectives"][0]["limit"] == 200
    assert result["objective_budget"]["objectives"][0]["actual_tokens"] == 80
    with service.store.transaction() as db:
        assert facade.store.get(db, "agent_stages", "budget-stage")["state"] == "active"
        assert facade.store.get(db, "agent_goals", work["goal_id"])["state"] == "active"
    assert service.snapshot(sid)["task"]["state"] == "queued"
    assert service.snapshot(sid)["task"]["limits"]["max_duration_seconds"] == 3600
    # A fresh ledger uses the granted allowance and retains previously settled usage.
    BudgetLedger(service.store.path).reserve("resumed-call", binding, 50, "execution")
    assert ledger.summary(aid)[0]["tokens"] == 130
    facade.supervisor.scheduler.advance(aid, manual=True)
    result = facade.snapshot(aid)
    assert len(result["objective_budgets"]["quality"]["grants"]) == 1
    assert result["work_packages"][0]["recovery"]["attempts"] == 1


@pytest.mark.parametrize("case", ["ceiling", "disabled", "daily", "goal", "manual_pause", "stage_paused", "invalid_legacy", "control", "attempts"])
def test_objective_recovery_is_bounded_and_does_not_override_other_blocks(agent_service, case):
    service, facade, aid, _, sid, _, _ = stopped_objective(agent_service)
    with service.store.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        if case == "ceiling":
            agent["role"]["scheduling_policy"]["max_objective_tokens"] = 100
        if case == "disabled":
            agent["role"]["scheduling_policy"]["auto_expand_objective_budget"] = False
        if case == "attempts":
            agent["role"]["scheduling_policy"]["max_recovery_attempts"] = 0
        if case in {"daily", "goal"}:
            agent["role"]["budget"][case + "_tokens"] = 100
        facade.store.put(db, "agents", agent)
        if case == "manual_pause":
            planning = facade.store.rows(db, "agent_objective_planning", aid)[0]
            planning["manual_pause"] = True
            facade.store.put(db, "agent_objective_planning", planning)
        if case == "stage_paused":
            stage = facade.store.get(db, "agent_stages", "budget-stage")
            stage["state"] = "paused"
            facade.store.put(db, "agent_stages", stage)
    if case == "invalid_legacy":
        service.store.update(sid, lambda s, e, db: s["run"]["failure"].update(message=s["run"]["failure"]["message"].replace("projected=130", "projected=129")))
    if case == "control":
        service.store.update(sid, lambda s, e, db: s.update(control=dict(command_id="human", kind="paused")))
    facade.supervisor.scheduler.advance(aid, manual=True)
    result = facade.snapshot(aid)
    assert result["scheduler"]["manual_required"]
    assert not result.get("objective_budgets")
    # Decision-level failures (budget ceilings, unrecognized reasons) consume a
    # recovery attempt; premise failures (manual control, paused stage, exhausted
    # ceiling) never do. A malformed legacy budget message parses to no block, so
    # the reviewing stage cannot be restored and recovery stops at that premise.
    expected = 1 if case in {"ceiling", "disabled", "daily", "goal"} else None
    assert (result["work_packages"][0].get("recovery") or {}).get("attempts") == expected
    if case == "attempts":
        assert result["work_packages"][0]["blocked_reason"] == "recovery_exhausted"
    assert service.snapshot(sid)["task"]["state"] == "failed"
    with service.store.transaction() as db:
        assert facade.store.get(db, "agent_stages", "budget-stage")["state"] == ("paused" if case == "stage_paused" else "reviewing")


def test_stage_review_waits_for_budget_recovery_instead_of_reviewing_failed_work(agent_service):
    _, facade, aid, _, _, _, _ = stopped_objective(agent_service, reviewing=False)
    assert facade.supervisor.stages._advance(aid, "quality") is False
    with facade.store.sessions.transaction() as db:
        assert facade.store.get(db, "agent_stages", "budget-stage")["state"] == "active"
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert facade.snapshot(aid)["scheduler"]["state"] == "recovered"


def test_effective_limit_honors_lowered_policy_and_initial_explicit_budget():
    objective = dict(id="quality", stage_planning=dict(objective_tokens=100))
    agent = dict(role=dict(budget=dict(goal_tokens=1000), scheduling_policy=dict(max_objective_tokens=150)),
                 objective_budgets=dict(quality=dict(tokens=200)))
    assert objective_limit(agent, objective) == 150
    agent["role"]["scheduling_policy"]["max_objective_tokens"] = 50
    assert objective_limit(agent, objective) == 100


def test_lost_recovery_reply_does_not_grant_objective_budget_twice(agent_service, monkeypatch):
    service, facade, aid, _, sid, _, _ = stopped_objective(agent_service)
    submit = service.submit

    def lost(*args, **kwargs):
        submit(*args, **kwargs)
        raise OSError("Lost response after accepted resume")

    monkeypatch.setattr(service, "submit", lost)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert service.snapshot(sid)["task"]["state"] == "queued"
    monkeypatch.setattr(service, "submit", submit)
    facade.supervisor.scheduler.advance(aid, manual=True)
    result = facade.snapshot(aid)
    assert result["objective_budgets"]["quality"]["tokens"] == 200
    assert len(result["objective_budgets"]["quality"]["grants"]) == 1
    assert result["work_packages"][0]["recovery"]["attempts"] == 1
