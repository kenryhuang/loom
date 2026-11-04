import pytest

from loom.autonomy.acceptance import refs
from loom.service.contracts import ServiceError
from tests.autonomy.test_stage_contracts import delivery
from tests.autonomy.test_stage_progression import setup_plan


def add(facade, aid, stage, **changes):
    body = dict(command_id="manual-goal", expected_revision=stage["revision"],
                goal=dict(objective="Check additional behavior", acceptance=["Additional check passes"]))
    return facade.dispatch("POST", [aid, "stages", stage["id"], "goals"], {}, {**body, **changes})


def revise(facade, aid, goal, **changes):
    body = dict(command_id="revise-goal", type="revise", expected_revision=goal["revision"],
                payload=dict(objective="Adjusted delivery", acceptance=["Adjusted check passes"]))
    return facade.goal_command(aid, goal["id"], {**body, **changes})


def test_manual_goal_is_scoped_ordered_versioned_and_replayable(agent_service):
    _, facade, aid, _ = agent_service
    before = setup_plan(facade, aid)
    stage = before["stages"][0]
    goal = add(facade, aid, stage)
    assert add(facade, aid, stage) == goal
    assert goal["stage_id"] == stage["id"] and goal["roadmap_id"] == stage["roadmap_id"]
    assert goal["long_term_objective_id"] == stage["objective_id"]
    assert goal["depends_on"] == stage["goal_ids"]
    assert goal["stop_conditions"] == stage["stop_conditions"]
    assert all(c in refs(goal["acceptance_criteria"]) for c in refs(stage["acceptance_criteria"]))
    after = facade.store.snapshot(aid)
    assert after["stages"][0]["goal_ids"] == [*stage["goal_ids"], goal["id"]]
    assert after["stages"][0]["revision"] == stage["revision"] + 1
    assert after["objective_planning"][0]["revision"] > before["objective_planning"][0]["revision"]
    with pytest.raises(ServiceError, match="Revision"):
        add(facade, aid, stage, command_id="stale")
    assert facade.store.snapshot(aid) == after


@pytest.mark.parametrize("status", ["completed", "cancelled", "superseded", "historical"])
def test_stage_history_rejects_add_and_revise_atomically(agent_service, status):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    stage = snap["stages"][0]
    goal = next(g for g in snap["goals"] if g["id"] in stage["goal_ids"])
    with facade.store.sessions.transaction() as db:
        if status == "historical":
            state = snap["objective_planning"][0]
            state["stage_id"] = None
            facade.store.put(db, "agent_objective_planning", state)
        else:
            stage["state"] = status
            facade.store.put(db, "agent_stages", stage)
    before = facade.store.snapshot(aid)
    for action in (lambda: add(facade, aid, stage), lambda: revise(facade, aid, goal)):
        with pytest.raises(ServiceError, match="historical"):
            action()
        assert facade.store.snapshot(aid) == before
    with pytest.raises(ServiceError, match="historical"):
        facade.goal_command(aid, goal["id"], dict(command_id="cancel-history", type="cancel", expected_revision=goal["revision"]))
    assert facade.store.snapshot(aid) == before


def test_stage_goal_changes_reject_cross_agent_and_unassigned_goals(agent_service):
    from tests.autonomy.helpers import role

    _, facade, aid, path = agent_service
    snap = setup_plan(facade, aid)
    other = facade.store.create("other", role(path))
    with pytest.raises(ServiceError, match="scope"):
        add(facade, other["id"], snap["stages"][0])
    goal = facade.store.add_goal(aid, "legacy", dict(objective="Legacy", acceptance=["Check"]))
    with pytest.raises(ServiceError, match="current unfinished stage"):
        revise(facade, aid, goal)
    with pytest.raises(ServiceError, match="removed"):
        facade.dispatch("POST", [aid, "goals"], {}, dict(command_id="standalone", goal=dict(objective="No", acceptance=["No"])))
    with pytest.raises(ServiceError, match="current unfinished stage"):
        facade.dispatch("POST", [aid, "goals", goal["id"], "retry"], {}, dict(
            command_id="legacy-retry", expected_revision=goal["revision"], change_kind="input", change="New input"))


def test_goal_revision_preserves_stage_constraints_and_retires_old_packages(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    stage = snap["stages"][0]
    goal = add(facade, aid, stage)
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, "agent_work_packages", dict(id="pending", agent_id=aid, goal_id=goal["id"], state="pending"))
    updated = revise(facade, aid, goal)
    assert updated["objective"] == "Adjusted delivery"
    for key in ("depends_on", "stage_id", "roadmap_id", "long_term_objective_id", "stop_conditions", "required_source_ids"):
        assert updated[key] == goal[key]
    assert all(c in refs(updated["acceptance_criteria"]) for c in refs(stage["acceptance_criteria"]))
    work = next(w for w in facade.store.snapshot(aid)["work_packages"] if w["id"] == "pending")
    assert work["superseded_contract"] and work["state"] == "cancelled"
    assert revise(facade, aid, goal) == updated
    with pytest.raises(ServiceError, match="Unknown"):
        revise(facade, aid, updated, command_id="move", payload=dict(objective="Move", acceptance=["Check"], long_term_objective_id="other"))


def test_goal_revision_does_not_change_in_flight_work(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    goal = next(g for g in snap["goals"] if g.get("stage_id"))
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, "agent_work_packages", dict(id="running", agent_id=aid, goal_id=goal["id"], state="running", session_id="session"))
    before = facade.store.snapshot(aid)
    with pytest.raises(ServiceError, match="current work"):
        revise(facade, aid, goal)
    assert facade.store.snapshot(aid) == before


def test_added_goal_requires_delivery_before_stage_review_can_complete(agent_service):
    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    stage = snap["stages"][0]
    for goal in snap["goals"]:
        if goal["id"] in stage["goal_ids"]:
            delivery(facade, aid, goal)
    goal = add(facade, aid, stage)
    facade.supervisor.stages.advance(aid)
    assert facade.store.snapshot(aid)["stages"][0]["state"] != "completed"
    delivery(facade, aid, goal)
    facade.supervisor.stages.advance(aid)
    assert facade.store.snapshot(aid)["stages"][0]["state"] == "completed"


def test_manual_add_supersedes_an_in_flight_stage_review(agent_service):
    from tests.autonomy.test_stage_end_to_end import decision

    _, facade, aid, _ = agent_service
    snap = setup_plan(facade, aid)
    for goal in snap["goals"]:
        if goal.get("stage_id"):
            delivery(facade, aid, goal)

    def concurrent_review(agent, payload):
        add(facade, aid, payload["stage"])
        return decision(agent, payload)

    facade.supervisor.stages.planner.provider = concurrent_review
    facade.supervisor.stages.advance(aid)
    after = facade.store.snapshot(aid)
    assert after["stages"][0]["state"] == "active"
    assert len(after["stages"][0]["goal_ids"]) == 2
    assert not after["stage_reviews"]
    with facade.store.sessions.read() as db:
        assert facade.store.rows(db, "agent_runs", aid)[-1]["state"] == "superseded"
