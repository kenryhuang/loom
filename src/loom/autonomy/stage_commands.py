"""Revision-checked human controls sharing the existing durable control outbox."""

from copy import deepcopy

from loom.autonomy.acceptance import criteria
from loom.autonomy.contracts import role_contract, stage_enabled, strict
from loom.autonomy.control import drain, enqueue
from loom.autonomy.stages import TERMINAL, planning_state
from loom.service.contracts import ServiceError, text


def reconcile_role_change(store, db, agent, previous_role, cid):
    """The setup form and planning API use the same switch and durable controls."""
    before = {o["id"]: stage_enabled(o) for o in previous_role["long_term_objectives"]}
    after = {o["id"]: stage_enabled(o) for o in agent["role"]["long_term_objectives"]}
    for state in store.rows(db, "agent_objective_planning", agent["id"]):
        oid = state["objective_id"]
        if before.get(oid, False) == after.get(oid, False):
            continue
        kind = "resume" if after.get(oid, False) else "pause"
        stage = store.get(db, "agent_stages", state["stage_id"], agent["id"]) if state.get("stage_id") else None
        ids = stage["goal_ids"] if stage and stage["state"] not in TERMINAL else []
        if state.get("discovery_id"):
            ids = [*ids, state["discovery_id"]]
        control_goals(store, db, agent["id"], ids, kind, cid)
        if stage and stage["state"] not in TERMINAL:
            if kind == "pause":
                stage.setdefault("paused_state", stage["state"])
                stage["state"] = "paused"
            else:
                stage["state"] = stage.pop("paused_state", "active")
            stage["revision"] += 1
            store.put(db, "agent_stages", stage)
        state.update(
            manual_pause=kind == "pause", state="paused" if kind == "pause" else "ready", blocker=None, retry_requested=False,
            revision=state["revision"] + 1, last_input=None, next_review_at=""
        )
        store.put(db, "agent_objective_planning", state)


def control_goals(store, db, aid, goal_ids, kind, cid):
    for gid in goal_ids:
        goal = store.get(db, "agent_goals", gid, aid)
        if goal["state"] in TERMINAL:
            continue
        works = [w for w in store.rows(db, "agent_work_packages", aid) if w["goal_id"] == gid and not w.get("superseded_contract")]
        if kind == "pause":
            goal.setdefault("stage_paused_state", goal["state"])
            goal["state"] = "paused"
        elif kind == "resume":
            goal["state"] = goal.pop("stage_paused_state", "active" if works else "queued")
        else:
            goal["state"] = "cancelled"
        goal["revision"] += 1
        store.put(db, "agent_goals", goal)
        for work in works:
            if work["state"] == "pending" and kind == "cancel":
                work["state"] = "cancelled"
                store.put(db, "agent_work_packages", work)
            if work.get("session_id") and work["state"] in {"running", "dispatched", "blocked"}:
                enqueue(db, aid, cid, work["session_id"], {"cancel": "stop_run", "pause": "pause", "resume": "resume"}[kind], gid)


def command(facade, aid, identifier, body, *, stage_command=False):
    strict(body, {"command_id", "expected_revision", "type", "reason", "question_id", "question_revision", "answer", "payload"})
    store, service = facade.store, facade.service
    with store.sessions.read() as db:
        agent = store.get(db, "agents", aid)
        if agent.get("planning_engine") == 2 and body.get("type") in {"replan", "revise"}:
            stage = store.get(db, "agent_stages", identifier, aid) if stage_command else None
            oid = stage["objective_id"] if stage else identifier
        else:
            oid = None
    if oid is not None:
        from loom.autonomy.plan_commands import request_revision

        return request_revision(facade, aid, oid, body, stage_id=identifier if stage_command else None)
    request = dict(agent_id=aid, identifier=identifier, stage_command=stage_command, **body)
    with service.lock:
        with store.sessions.transaction() as db:
            replay = store.replay(db, body.get("command_id"), request)
            if replay:
                return replay
            agent = store.get(db, "agents", aid)
            stage = store.get(db, "agent_stages", identifier, aid) if stage_command else None
            oid = stage["objective_id"] if stage else identifier
            objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == oid), None)
            if objective is None:
                raise ServiceError("Unknown long-term objective", 404)
            state = planning_state(store, db, agent, objective)
            kind = body.get("type")
            store.expect(stage if stage else agent if kind in {"enable", "disable"} else state, body.get("expected_revision"))
            current_stage = stage or (store.get(db, "agent_stages", state["stage_id"], aid) if state.get("stage_id") else None)
            if stage and (stage["state"] in TERMINAL or state.get("stage_id") != stage["id"]):
                raise ServiceError("Terminal or historical stage is immutable; replan the objective", 409)
            allowed = {"pause", "resume", "skip", "cancel", "revise"} if stage else {"enable", "disable", "replan", "answer", "pause", "resume"}
            if kind not in allowed:
                raise ServiceError("Unknown stage planning command")
            if kind in {"skip", "cancel", "revise", "replan"}:
                reason = text(body.get("reason"), "changed input or reason", max_length=4000)
                if reason in state.get("replan_reasons", []):
                    raise ServiceError("This change was already used to replan; provide new input", 409)
                state.setdefault("replan_reasons", []).append(reason)
                state["answers"].append(dict(type=kind, text=reason, producer="human"))
            if kind in {"enable", "disable"}:
                raw = deepcopy(agent["role"])
                raw.pop("schema_version", None)
                target = next(o for o in raw["long_term_objectives"] if o["id"] == oid)
                target["stage_planning"] = {**target.get("stage_planning", {}), "mode": "autonomous" if kind == "enable" else "off"}
                agent.update(role=role_contract(raw), role_revision=agent["role_revision"] + 1, revision=agent["revision"] + 1)
                store.put(
                    db,
                    "agent_roles",
                    dict(
                        id=f"{aid}:{agent['role_revision']}",
                        agent_id=aid,
                        revision=agent["role_revision"],
                        role=agent["role"],
                        explicit_fields=["long_term_objectives"],
                        derived_fields=[],
                    ),
                )
                kind = "resume" if kind == "enable" else "pause"
                for work in store.rows(db, "agent_work_packages", aid):
                    if work["state"] == "pending":
                        work["role_revision"] = agent["role_revision"]
                        store.put(db, "agent_work_packages", work)
            if kind == "answer":
                question = state.get("question")
                if (
                    state["state"] != "awaiting_input"
                    or not question
                    or question["id"] != body.get("question_id")
                    or question["revision"] != body.get("question_revision")
                ):
                    raise ServiceError("Question is stale or no longer pending", 409)
                answer = text(body.get("answer"), "answer", max_length=4000)
                state["answers"].append(dict(question=question, text=answer, producer="human"))
                if question.get("field") == "completion_criteria":
                    objective["stage_planning"]["completion_criteria"] = [answer]
                    agent.update(role_revision=agent["role_revision"] + 1, revision=agent["revision"] + 1)
                    store.put(db, "agent_roles", dict(id=f"{aid}:{agent['role_revision']}", agent_id=aid, revision=agent["role_revision"], role=agent["role"]))
                state.pop("question", None)
                state.update(state="ready", last_input=None, next_review_at="", blocker=None, retry_requested=False)
            elif kind in {"pause", "resume"}:
                if kind == "resume" and current_stage and current_stage["state"] == "blocked":
                    raise ServiceError("Blocked stage needs changed input and replan", 409)
                state.update(manual_pause=kind == "pause", state="paused" if kind == "pause" else "ready", blocker=None,
                             retry_requested=False, last_input=None, next_review_at="")
                goal_ids = current_stage["goal_ids"] if current_stage and current_stage["state"] not in TERMINAL else []
                if state.get("discovery_id"):
                    goal_ids = [*goal_ids, state["discovery_id"]]
                control_goals(store, db, aid, goal_ids, kind, body["command_id"])
                if current_stage and current_stage["state"] not in TERMINAL:
                    current_stage["state"] = "paused" if kind == "pause" else "active"
                    current_stage["revision"] += 1
                    store.put(db, "agent_stages", current_stage)
            elif kind in {"replan", "revise", "skip", "cancel"}:
                if current_stage and current_stage["state"] not in TERMINAL:
                    if kind in {"replan", "revise"}:
                        work = [w for w in store.rows(db, "agent_work_packages", aid) if w["goal_id"] in current_stage["goal_ids"]]
                        if any(w.get("session_id") and w["state"] in {"running", "dispatched", "blocked"} for w in work):
                            raise ServiceError("Finish or cancel in-flight stage work before replacing its contract", 409)
                    control_goals(store, db, aid, current_stage["goal_ids"], "cancel", body["command_id"])
                    current_stage.update(state="cancelled" if kind == "cancel" else "superseded", reason=body["reason"], revision=current_stage["revision"] + 1)
                    store.put(db, "agent_stages", current_stage)
                if kind == "revise":
                    payload = strict(body.get("payload", {}), {"acceptance", "expected_outcome"})
                    from loom.autonomy.contracts import strings

                    if not strings(payload.get("acceptance", []), "revised acceptance"):
                        raise ServiceError("Revised stage requires acceptance")
                    text(payload.get("expected_outcome"), "expected_outcome", max_length=4000)
                    state["required_revision"] = dict(payload, acceptance_criteria=criteria(
                        state["id"] + ":revision", payload["acceptance"], state["revision"] + 1))
                state.update(state="cancelled" if kind == "cancel" else "ready", manual_pause=False, last_input=None, next_review_at="",
                             blocker=None, retry_requested=False)
                # Explicit new input permits another bounded attempt, but neither paid usage nor daily stage counters reset.
                state["no_progress"] = max(0, state["no_progress"] - 1)
            state["revision"] += 1
            store.put(db, "agent_objective_planning", state)
            agent.update(generation=agent["generation"] + 1, next_wake_at=store.now(), planner_lease=None)
            if agent.get("planning_engine") == 2:
                from loom.autonomy.planning import wake

                wake(store, agent)
            store.put(db, "agents", agent)
            store.event(db, aid, "stage.command", dict(type=body["type"], objective_id=oid, stage_id=stage and stage["id"]))
            result = store.receipt(db, body["command_id"], request, state)
        drain(service, store, aid)
        return result
