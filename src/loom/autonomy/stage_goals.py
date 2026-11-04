"""Human Goal changes are scoped to the current, unfinished stage."""

from loom.autonomy.acceptance import inherit
from loom.autonomy.contracts import check_dependencies, goal_contract, strict
from loom.autonomy.stages import TERMINAL, planning_state
from loom.service.contracts import ServiceError, new_id


def editable_stage(store, db, agent, sid, *, goal=None):
    if not sid:
        raise ServiceError("Goal changes require a current unfinished stage", 409)
    stage = store.get(db, "agent_stages", sid, agent["id"])
    objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == stage["objective_id"]), None)
    if objective is None:
        raise ServiceError("Unknown long-term objective", 409)
    state = planning_state(store, db, agent, objective)
    if stage["state"] in TERMINAL or state.get("stage_id") != sid:
        raise ServiceError("Terminal or historical stage is immutable", 409)
    if goal and (goal["id"] not in stage["goal_ids"] or goal["long_term_objective_id"] != stage["objective_id"]):
        raise ServiceError("Goal is not part of the current stage contract", 409)
    return stage, state


def changed(store, db, stage, state):
    # Invalidate in-flight reviews, while retaining pauses and other blockers.
    if stage["state"] == "reviewing":
        stage["state"] = "active"
    stage["revision"] += 1
    store.put(db, "agent_stages", stage)
    state.update(revision=state["revision"] + 1, last_input=None, next_review_at="")
    if state["state"] == "reviewing":
        state["state"] = "active"
    store.put(db, "agent_objective_planning", state)


def add(facade, aid, sid, body):
    strict(body, {"command_id", "expected_revision", "goal"})
    request = dict(agent_id=aid, stage_id=sid, type="stage_goal", **body)
    store = facade.store
    with facade.service.lock, store.sessions.transaction() as db:
        replay = store.replay(db, body.get("command_id"), request)
        if replay:
            return replay
        agent = store.get(db, "agents", aid)
        stage, state = editable_stage(store, db, agent, sid)
        store.expect(stage, body.get("expected_revision"))
        raw = strict(body.get("goal", {}), {"objective", "acceptance"})
        goal = dict(
            **goal_contract(dict(
                **raw, long_term_objective_id=stage["objective_id"], depends_on=list(stage["goal_ids"]),
                stop_conditions=stage.get("stop_conditions", []), required_source_ids=stage.get("required_source_ids", []),
            ), agent["role"]),
            id=new_id("goal"), agent_id=aid, revision=1, state="queued", created_at=store.now(),
            producer="human", stage_id=sid, roadmap_id=stage["roadmap_id"],
        )
        # The new tail must verify the whole stage after the added delivery.
        inherit(goal, stage["acceptance_criteria"])
        check_dependencies([*store.rows(db, "agent_goals", aid), goal])
        store.put(db, "agent_goals", goal)
        stage["goal_ids"].append(goal["id"])
        changed(store, db, stage, state)
        agent.update(generation=agent["generation"] + 1, next_wake_at=store.now())
        store.put(db, "agents", agent)
        store.event(db, aid, "goal.created", dict(goal_id=goal["id"], stage_id=sid, producer="human"))
        return store.receipt(db, body.get("command_id"), request, goal)
