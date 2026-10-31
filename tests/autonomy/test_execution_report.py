from loom.autonomy.sources import refresh
from tests.autonomy.helpers import goals


def test_blocked_source_includes_bound_path_actual_error_and_recovery(agent_service):
    _, facade, aid, _ = agent_service
    goal = facade.store.add_goal(aid, "goal", {**goals()[0], "required_source_ids": ["code"]})
    refresh(facade.store, aid, force=True)
    report = facade.snapshot(aid)["execution"]
    block = next(b for b in report["blockers"] if b["code"] == "SOURCE_NOT_CURRENT")
    assert block["scope_id"] == goal["id"] + ":code"
    assert "src, tests" in " ".join(block["details"])
    assert "unavailable" in " ".join(block["details"])
    assert block["action"] == "refresh_sources"
    assert len(block["next_steps"]) == 2


def test_work_predecessor_and_analysis_failure_are_actionable(agent_service):
    _, facade, aid, _ = agent_service
    goal = facade.store.add_goal(aid, "goal", goals()[0])
    with facade.store.sessions.transaction() as db:
        for order in range(2):
            facade.store.put(
                db, "agent_work_packages", dict(id=f"w{order}", agent_id=aid, goal_id=goal["id"], objective=f"Work {order}", state="pending", order=order)
            )
        facade.store.put(
            db,
            "agent_learning_cycles",
            dict(id="c", agent_id=aid, state="failed", attempts=6, blocked_reason="Analysis attempt limit reached: 6/6; accepted memories retained"),
        )
    blocks = facade.snapshot(aid)["execution"]["blockers"]
    dependency = next(b for b in blocks if b["code"] == "WORK_DEPENDENCY")
    assert dependency["scope_id"] == "w1" and "Work 0" in dependency["details"][0]
    analysis = next(b for b in blocks if b["code"] == "ANALYSIS_FAILED")
    assert "6/6" in analysis["reason"] and not analysis["automatic"]
    assert analysis["next_steps"]
