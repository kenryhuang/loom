import json

import pytest

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.contracts import role_contract
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import Clock, role


def configure(facade, aid, **policy):
    facade.store.clock = Clock()
    agent = facade.store.snapshot(aid)
    value = {k: v for k, v in agent["role"].items() if k != "schema_version"}
    value["long_term_objectives"][0]["stage_planning"] = dict(mode="autonomous", **policy)
    return facade.store.revise_role(aid, "stage-role", agent["revision"], value)


def delivery(facade, aid, goal=None, *, passed=True):
    store = facade.store
    goal = goal or store.add_goal(aid, "baseline", dict(objective="Baseline file inspection", acceptance=["Recorded baseline"]))
    gid = goal["id"]
    sid, rid, wid = "session-" + gid, "run-" + gid, "work-" + gid
    with store.sessions.transaction() as db:
        goal.update(state="completed" if passed else "blocked", revision=goal["revision"] + 1)
        store.put(db, "agent_goals", goal)
        work = dict(
            id=wid,
            agent_id=aid,
            goal_id=gid,
            state="succeeded" if passed else "failed",
            objective=goal["objective"],
            acceptance=goal["acceptance"],
            session_id=sid,
            terminal_run_id=rid,
            run_ids=[rid],
            outcome=dict(event_cursor=2, acceptance="passed" if passed else "unknown"),
            role_revision=store.get(db, "agents", aid)["role_revision"],
            created_at=store.now(),
            order=0,
        )
        store.put(db, "agent_work_packages", work)
        for seq, kind, payload in [
            (1, "tool.completed", dict(output="Recorded implementation")),
            (2, "acceptance.gate.passed" if passed else "acceptance.gate.blocked", {}),
        ]:
            db.execute("INSERT OR REPLACE INTO events VALUES(?,?,?)", (sid, seq, json.dumps(dict(seq=seq, run_id=rid, type=kind, payload=payload))))
    return goal


@pytest.mark.parametrize("field,value", [("horizon_stages", 4), ("max_goals_per_stage", True), ("objective_tokens", 0), ("mode", "always")])
def test_stage_policy_rejects_invalid_bounds(tmp_path, field, value):
    configured = role(tmp_path)
    configured["long_term_objectives"][0]["stage_planning"] = {field: value}
    with pytest.raises(ServiceError):
        role_contract(configured)
    assert "stage_planning" not in role_contract(role(tmp_path))["long_term_objectives"][0]


def test_objective_budget_includes_legacy_planning_execution_learning_and_unknown_across_days(agent_service):
    _, facade, aid, _ = agent_service
    goal = facade.store.add_goal(aid, "old-goal", dict(objective="Old delivery", acceptance=["checked"]))
    ledger = BudgetLedger(facade.store.sessions.path)
    ledger.reserve("old", dict(agent_id=aid, goal_id=goal["id"]), 20, "execution")
    ledger.settle("old", 10)
    configure(facade, aid, objective_tokens=100)
    ledger.clock = facade.store.clock
    ledger.reserve("planning", dict(agent_id=aid, objective_id="quality"), 40, "stage_planning")
    ledger.settle("planning", None)
    ledger.reserve("learning", dict(agent_id=aid, goal_id=goal["id"]), 30, "learning")
    facade.store.clock.advance(86400)
    with pytest.raises(ServiceError, match="objective token budget"):
        ledger.reserve("over", dict(agent_id=aid, objective_id="quality"), 21, "stage_planning")
    with pytest.raises(ServiceError, match="identity conflict"):
        ledger.reserve("planning", dict(agent_id=aid, objective_id="other"), 40, "stage_planning")
    summary = ledger.objective_summary(aid)["objectives"][0]
    assert summary["actual_tokens"] == 10 and summary["reserved_or_unknown"] == 70 and summary["calls"] == 3


def test_stage_sources_and_schedules_do_not_duplicate_goals(agent_service):
    from loom.autonomy.goals import materialize, observe
    from loom.autonomy.triggers import materialize as schedules

    _, facade, aid, _ = agent_service
    agent = configure(facade, aid)
    value = {k: v for k, v in agent["role"].items() if k != "schema_version"}
    value["long_term_objectives"][0].update(iteration=dict(acceptance=["checked"]), schedule=dict(every_seconds=10, acceptance=["checked"]))
    facade.store.revise_role(aid, "schedule", agent["revision"], value)
    observe(facade.store, aid, "changed", dict(objective_id="quality", indicator="code", state="gap", source="human"))
    materialize(facade.store, aid)
    schedules(facade.store, aid)
    facade.supervisor.stages.advance(aid)
    assert len(facade.store.snapshot(aid)["goals"]) == 1
    assert facade.store.snapshot(aid)["goals"][0]["producer"] == "stage_discovery"


def test_method_induction_shares_learning_allowance_with_deep_eval(agent_service):
    _, facade, aid, _ = agent_service
    agent = configure(facade, aid)
    role_value = {k: v for k, v in agent["role"].items() if k != "schema_version"}
    role_value["budget"]["learning_tokens"] = 100
    facade.store.revise_role(aid, "learning-cap", agent["revision"], role_value)
    ledger = BudgetLedger(facade.store.sessions.path, clock=facade.store.clock)
    ledger.reserve("method", dict(agent_id=aid, objective_id="quality"), 80, "method_learning")
    with pytest.raises(ServiceError, match="learning token budget"):
        ledger.reserve("eval", dict(agent_id=aid, objective_id="quality"), 21, "learning")
