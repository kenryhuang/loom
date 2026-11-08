"""Versioned controls and safe migration into the single planning pipeline."""

import sqlite3
from copy import deepcopy

from loom.autonomy.acceptance import criteria
from loom.autonomy.contracts import strict, strings
from loom.autonomy.planning import wake
from loom.autonomy.stages import TERMINAL, planning_state
from loom.autonomy.store import digest
from loom.service.contracts import ServiceError, text


def retry_goal(facade, aid, gid, body):
    strict(body, {"command_id", "expected_revision", "change", "change_kind"})
    if body.get("change_kind") not in {"input", "method", "environment"}:
        raise ServiceError("Retry must identify input, method or environment change")
    request = dict(agent_id=aid, goal_id=gid, **body)
    with facade.store.sessions.read() as db:
        replay = facade.store.replay(db, body.get("command_id"), request)
        if replay:
            return replay
        goal = facade.store.get(db, "agent_goals", gid, aid)
        facade.store.expect(goal, body.get("expected_revision"))
        if goal["state"] != "blocked":
            raise ServiceError("Only blocked goals need an execution retry", 409)
        stage = facade.store.get(db, "agent_stages", goal["stage_id"], aid)
    return request_revision(facade, aid, goal["long_term_objective_id"], dict(command_id=body["command_id"],
        expected_revision=stage["revision"], reason=text(body.get("change"), "changed input", max_length=4000)),
        stage_id=stage["id"], receipt_request=request)


def request_revision(facade, aid, oid, body, *, stage_id=None, addition=None, receipt_request=None):
    store = facade.store
    request = receipt_request or dict(agent_id=aid, objective_id=oid, stage_id=stage_id, addition=addition, **body)
    with facade.service.lock, store.sessions.transaction() as db:
        replay = store.replay(db, body.get("command_id"), request)
        if replay:
            return replay
        agent = store.get(db, "agents", aid)
        if agent.get("planning_engine") != 2:
            raise ServiceError("This command requires the unified planning engine", 409)
        objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == oid), None)
        if not objective:
            raise ServiceError("Unknown objective", 404)
        state = planning_state(store, db, agent, objective)
        stage = store.get(db, "agent_stages", state["stage_id"], aid) if state.get("stage_id") else None
        if stage_id and (not stage or stage["id"] != stage_id or stage["state"] in TERMINAL):
            raise ServiceError("Only the current unfinished stage can be revised", 409)
        store.expect(stage if stage_id else state, body.get("expected_revision"))
        if stage:
            for work in store.rows(db, "agent_work_packages", aid):
                if work.get("plan_id") != stage.get("plan_id") or not work.get("session_id"):
                    continue
                session = store.sessions._load(db, work["session_id"])
                if work["session_id"] in facade.service.active or session["task"]["state"] in {"running", "queued", "pausing"}:
                    raise ServiceError("Wait for execution to stop before requesting a plan revision", 409)
        reason = text(body.get("reason"), "changed input or reason", max_length=4000)
        if reason in state.get("replan_reasons", []):
            raise ServiceError("This input already requested a revision; provide new information", 409)
        state.setdefault("replan_reasons", []).append(reason)
        state["answers"].append(dict(type="replan", text=reason, producer="human"))
        raw = strict(body.get("payload", {}), {"acceptance", "expected_outcome"})
        if raw:
            acceptance = strings(raw.get("acceptance", []), "revised acceptance")
            if not acceptance:
                raise ServiceError("A revised stage requires acceptance")
            state["required_revision"] = dict(raw, acceptance_criteria=criteria(state["id"] + ":revision", acceptance, state["revision"] + 1))
        if addition:
            strict(addition, {"objective", "acceptance"})
            text(addition.get("objective"), "Goal objective")
            if not strings(addition.get("acceptance", []), "Goal acceptance"):
                raise ServiceError("A requested Goal needs acceptance criteria")
            state.setdefault("requested_goals", []).append(addition)
        state.update(revision_requested=True, state="ready", last_input=None, next_review_at="", blocker=None,
                     revision=state["revision"] + 1, no_progress=max(0, state["no_progress"] - 1))
        store.put(db, "agent_objective_planning", state)
        agent.update(generation=agent["generation"] + 1, planner_lease=None)
        wake(store, agent)
        store.put(db, "agents", agent)
        store.event(db, aid, "plan.revision.requested", dict(objective_id=oid, stage_id=stage and stage["id"], reason=reason))
        return store.receipt(db, body["command_id"], request, state)


def migration_report(store, db, aid):
    agent = store.get(db, "agents", aid)
    works = store.rows(db, "agent_work_packages", aid)
    blockers = []
    if agent.get("planner_lease"):
        blockers.append("A planning call is in flight")
    for work in works:
        if not work.get("session_id"):
            continue
        session = store.sessions._load(db, work["session_id"])
        if session["task"]["state"] in {"running", "queued", "pausing", "paused", "awaiting_input"}:
            blockers.append("Resolve execution before migration: " + work["session_id"])
        if session.get("workspace_blocked") or db.execute(
                "SELECT 1 FROM operations WHERE session_id=? AND json_extract(body,'$.status')='started' "
                "AND json_extract(body,'$.effect_kind')='side_effecting'", (work["session_id"],)).fetchone():
            blockers.append("Unconfirmed execution result: " + work["session_id"])
    return dict(agent_id=aid, revision=agent["revision"], engine=agent.get("planning_engine", 1),
                blockers=list(dict.fromkeys(blockers)),
                retire_work_ids=[w["id"] for w in works if w["state"] not in {"succeeded", "cancelled"}],
                preserve_completed_goal_ids=[g["id"] for g in store.rows(db, "agent_goals", aid) if g["state"] == "completed"])


def migrate(facade, aid, body):
    strict(body, {"command_id", "expected_revision"})
    store = facade.store
    request = dict(agent_id=aid, type="migrate_planning", **body)
    with facade.service.lock:
        with store.sessions.read() as db:
            replay = store.replay(db, body.get("command_id"), request)
            if replay:
                return replay
            report = migration_report(store, db, aid)
            if report["blockers"]:
                raise ServiceError("; ".join(report["blockers"]), 409)
            store.expect(report, body.get("expected_revision"))
        backup = store.sessions.directory / ("before-plan-v2-" + digest(body["command_id"])[:16] + ".sqlite")
        if not backup.exists():
            with sqlite3.connect(store.sessions.path) as source, sqlite3.connect(backup) as target:
                source.backup(target)
        with store.sessions.transaction() as db:
            current = migration_report(store, db, aid)
            if current != report:
                raise ServiceError("Agent changed while preparing migration; retry with the current revision", 409)
            agent = store.get(db, "agents", aid)
            if agent.get("planning_engine") == 2:
                return store.receipt(db, body["command_id"], request, dict(report, migrated=False, backup=str(backup)))
            for work in store.rows(db, "agent_work_packages", aid):
                if work["state"] not in {"succeeded", "cancelled"}:
                    work.update(state="cancelled", superseded_contract=True, migration_reason="Recompile under unified planning")
                    store.put(db, "agent_work_packages", work)
                    db.execute("UPDATE agent_outbox SET state='superseded' WHERE id=?", (work["id"],))
            for goal in store.rows(db, "agent_goals", aid):
                if goal["state"] not in TERMINAL:
                    goal.update(state="superseded", revision=goal["revision"] + 1)
                    store.put(db, "agent_goals", goal)
            for stage in store.rows(db, "agent_stages", aid):
                if stage["state"] not in TERMINAL:
                    stage.update(state="superseded", revision=stage["revision"] + 1)
                    store.put(db, "agent_stages", stage)
            for objective in agent["role"]["long_term_objectives"]:
                state = planning_state(store, db, agent, objective)
                previous = deepcopy(state)
                target = state["state"] if state["state"] in {"completed", "cancelled"} else "paused" if state.get("manual_pause") else "ready"
                state.update(engine_version=2, state=target, stage_id=None,
                             roadmap_id=None, discovery_id=None, last_input=None, next_review_at="", blocker=None,
                             revision=state["revision"] + 1, migration_from=previous)
                store.put(db, "agent_objective_planning", state)
            agent.update(planning_engine=2, revision=agent["revision"] + 1, generation=agent["generation"] + 1,
                         error=None, scheduler={}, planner_lease=None)
            wake(store, agent)
            store.put(db, "agents", agent)
            result = dict(report, migrated=True, backup=str(backup))
            store.event(db, aid, "planning.migrated", result)
            return store.receipt(db, body["command_id"], request, result)
