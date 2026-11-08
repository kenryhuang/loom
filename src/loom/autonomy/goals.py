"""Evidence-driven obligations, explainable ranking and bounded successor attempts."""

from datetime import datetime, timedelta

from loom.autonomy.contracts import goal_contract, strict
from loom.autonomy.store import digest
from loom.service.contracts import ServiceError, text


def observe(store, aid, cid, payload, *, producer="human"):
    strict(payload, {"objective_id", "indicator", "value", "state", "source", "object_id", "version"})
    if payload.get("state") not in {"met", "gap", "unknown"}:
        raise ServiceError("Observation state must be met, gap or unknown")
    text(payload.get("source"), "observation source", max_length=2000)
    with store.sessions.transaction() as db:
        request = dict(agent_id=aid, producer=producer, payload=payload)
        replay = store.replay(db, cid, request)
        if replay:
            return replay
        agent = store.get(db, "agents", aid)
        objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == payload.get("objective_id")), None)
        if not objective:
            raise ServiceError("Unknown objective")
        indicator = text(payload.get("indicator"), "indicator", max_length=200)
        if objective["indicators"] and indicator not in objective["indicators"]:
            raise ServiceError("Indicator outside objective")
        row = dict(
            **payload,
            id="observation_" + digest([aid, producer, payload]),
            agent_id=aid,
            producer=producer,
            created_at=store.now(),
            role_revision=agent["role_revision"],
        )
        store.put(db, "agent_observations", row)
        agent.update(next_wake_at=store.now(), generation=agent["generation"] + 1)
        if agent.get("planning_engine") == 2:
            from loom.autonomy.planning import wake

            wake(store, agent)
        store.put(db, "agents", agent)
        store.event(db, aid, "objective.observed", dict(observation_id=row["id"], state=row["state"]))
        return store.receipt(db, cid, request, row)


def materialize(store, aid):
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        if agent["state"] != "enabled" or agent.get("planning_engine") == 2:
            return
        observations = store.rows(db, "agent_observations", aid)
        goals = store.rows(db, "agent_goals", aid)
        latest = {}
        for row in observations:
            latest[(row["objective_id"], row["indicator"], row.get("object_id", ""))] = row
        states = {}
        for objective in agent["role"]["long_term_objectives"]:
            rows = [r for key, r in latest.items() if key[0] == objective["id"]]
            from loom.autonomy.contracts import stage_enabled

            if stage_enabled(objective):
                continue
            iteration = objective.get("iteration")
            review_due = (
                objective["mode"] == "ongoing"
                and iteration
                and rows
                and all((store.clock() - datetime.fromisoformat(r["created_at"])).total_seconds() >= iteration["review_seconds"] for r in rows)
            )
            complete = bool(rows) and all(r["state"] == "met" for r in rows)
            if objective["indicators"]:
                complete = complete and set(objective["indicators"]) <= {r["indicator"] for r in rows}
            states[objective["id"]] = dict(
                state="met" if complete else "gap" if any(r["state"] == "gap" for r in rows) else "unknown",
                observation_ids=[r["id"] for r in rows],
                mode=objective["mode"],
            )
            if review_due:
                states[objective["id"]]["state"] = "unknown"
                states[objective["id"]]["reason"] = "Periodic evidence review is due"
            if (
                not iteration
                or complete
                and not review_due
                or any(g["long_term_objective_id"] == objective["id"] and g["state"] not in {"completed", "cancelled", "superseded"} for g in goals)
            ):
                continue
            for row in rows:
                # Unknown evidence requests verification, never a fabricated deficit.
                if row["producer"] not in {"human", "bound_source", "external_event"} or row["state"] == "met" and not review_due:
                    continue
                if row.get("role_revision") != agent["role_revision"]:
                    continue
                key = [aid, objective["id"], row.get("object_id", ""), row["id"]]
                if review_due:
                    key.append(int((store.clock() - datetime.fromisoformat(row["created_at"])).total_seconds() // iteration["review_seconds"]))
                gid = "goal_proposal_" + digest(key)[:32]
                if any(g["id"] == gid for g in goals):
                    continue
                goal = dict(
                    **goal_contract(
                        dict(objective=objective["desired_outcome"], acceptance=iteration["acceptance"], long_term_objective_id=objective["id"]), agent["role"]
                    ),
                    id=gid,
                    agent_id=aid,
                    revision=1,
                    state="queued",
                    created_at=store.now(),
                    producer="goal_proposal",
                    proposal=dict(
                        observation_id=row["id"], source=row["source"], reason=f"{row['indicator']}: {row['state']}", depth=1, object_id=row.get("object_id")
                    ),
                    not_before=store.now(),
                )
                store.put(db, "agent_goals", goal)
                goals.append(goal)
                agent.update(generation=agent["generation"] + 1, next_wake_at=store.now())
                store.event(db, aid, "goal.proposed", dict(goal_id=gid, observation_id=row["id"]))
                break  # At most one new goal per objective and tick.
        agent["objective_states"] = states
        store.put(db, "agents", agent)


def rank(store, db, agent, goals, packages):
    from loom.autonomy.blockers import Code, make

    done = {g["id"] for g in goals if g["state"] == "completed"}
    ranked, reasons = [], []
    for goal in goals:
        from loom.autonomy.stages import runnable

        reason, code = None, None
        if goal["state"] != "queued":
            reason, code = "Goal is " + goal["state"], Code.GOAL_STATE
        elif not runnable(store, db, agent, goal):
            reason, code = "Stage is paused, blocked or automatic planning is disabled", Code.STAGE_STATE
        elif not set(goal["depends_on"]) <= done:
            reason, code = "Await required goals", Code.DEPENDENCY_WAIT
        elif goal.get("not_before", "") > store.now():
            reason, code = "Failure backoff; wait until " + goal["not_before"], Code.RETRY_BACKOFF
        elif not sources_ready(store, db, agent, goal):
            reason, code = "Refresh required source routes", Code.SOURCE_UNAVAILABLE
        elif any(w["goal_id"] == goal["id"] and not w.get("superseded_contract") for w in packages):
            reason, code = "Existing execution contract", Code.WORK_EXISTS
        age = max(0, (store.clock() - datetime.fromisoformat(goal["created_at"])).total_seconds())
        blocked = sum(goal["id"] in g["depends_on"] for g in goals if g["state"] == "queued")
        deadline = datetime.fromisoformat(goal["deadline"]).timestamp() if goal.get("deadline") else float("inf")
        score = goal["priority"] + min(100, age / 3600) + min(50, blocked * 10)
        score += max(0, min(144, 72 - (deadline - store.clock().timestamp()) / 3600))
        reasons.append(
            dict(
                goal_id=goal["id"],
                reason=reason or "Ready: priority, deadline, dependent goals and waiting time",
                eligible=code is None,
                blocker=make(code, goal["id"], reason, phase="goal_selection") if code else None,
                score=score,
                blocked_goals=blocked,
                waiting_seconds=int(age),
            )
        )
        if code is None:
            ranked.append((-score, deadline, goal["created_at"], goal))
    ranked.sort(key=lambda row: row[:3])
    return [row[-1] for row in ranked], reasons


def sources_ready(store, db, agent, goal):
    sources = {s["route_id"]: s for s in store.rows(db, "agent_sources", agent["id"])}
    routes = {r["id"]: r for r in agent["role"]["source_routes"]}

    def current(identifier):
        row = sources.get(identifier, {})
        return row.get("state") == "current" and row.get("expires_at", "") > store.now() and row.get("role_revision") == agent["role_revision"]

    return all(current(s) or any(current(f) for f in routes.get(s, {}).get("fallback_route_ids", [])) for s in goal.get("required_source_ids", []))


def retry(store, aid, gid, body, *, stage_only=False):
    strict(body, {"command_id", "expected_revision", "change", "change_kind"})
    if body.get("change_kind") not in {"input", "method", "environment"}:
        raise ServiceError("Retry must identify input, method or environment change")
    change = text(body.get("change"), "change", max_length=4000)
    with store.sessions.transaction() as db:
        request = dict(agent_id=aid, goal_id=gid, **body)
        old = store.replay(db, body.get("command_id"), request)
        if old:
            return old
        agent = store.get(db, "agents", aid)
        goal = store.get(db, "agent_goals", gid, aid)
        if agent.get("planning_engine") == 2:
            raise ServiceError("Compiled goals are retried through a complete plan revision", 409, code="EXECUTION_CONTRACT")
        store.expect(goal, body.get("expected_revision"))
        if stage_only:
            from loom.autonomy.stage_goals import editable_stage

            editable_stage(store, db, agent, goal.get("stage_id"), goal=goal)
        if goal["state"] != "blocked":
            raise ServiceError("Only blocked goals can create successor attempts", 409)
        depth = goal.get("attempt", 0) + 1
        limit = next(o.get("iteration", {}).get("max_depth", 2) for o in agent["role"]["long_term_objectives"] if o["id"] == goal["long_term_objective_id"])
        if depth > limit:
            raise ServiceError("Attempt limit reached; revise the goal", 409)
        fingerprint = digest([goal.get("retry_root", gid), body["change_kind"], change])
        goals = store.rows(db, "agent_goals", aid)
        if any(g.get("change_fingerprint") == fingerprint or g.get("predecessor_id") == gid for g in goals):
            raise ServiceError("Successor already exists or change already attempted", 409)
        new = {
            **goal,
            "id": "goal_retry_" + fingerprint[:32],
            "revision": 1,
            "state": "queued",
            "created_at": store.now(),
            "attempt": depth,
            "retry_root": goal.get("retry_root", gid),
            "predecessor_id": gid,
            "change_fingerprint": fingerprint,
            "change": change,
            "change_kind": body["change_kind"],
            "not_before": (store.clock() + timedelta(seconds=60 * 2 ** (depth - 1))).isoformat(),
        }
        store.put(db, "agent_goals", new)
        if goal.get('stage_id'):
            from loom.autonomy.stages import planning_state

            stage = store.get(db, 'agent_stages', goal['stage_id'], aid)
            if stage['state'] in {'completed', 'cancelled', 'superseded'} or gid not in stage['goal_ids']:
                raise ServiceError('Retry requires the current stage contract', 409)
            objective = next(o for o in agent['role']['long_term_objectives'] if o['id'] == goal['long_term_objective_id'])
            state = planning_state(store, db, agent, objective)
            if state['no_progress'] >= objective['stage_planning']['max_replans_without_progress']:
                raise ServiceError('Stage no-progress limit reached; replan with new input', 409)
            stage.update(goal_ids=[new['id'] if value == gid else value for value in stage['goal_ids']],
                         state='active', revision=stage['revision'] + 1)
            store.put(db, 'agent_stages', stage)
            for dependent in goals:
                if dependent['id'] in stage['goal_ids'] and gid in dependent['depends_on'] and dependent['state'] == 'queued':
                    dependent.update(depends_on=[new['id'] if value == gid else value for value in dependent['depends_on']], revision=dependent['revision'] + 1)
                    store.put(db, 'agent_goals', dependent)
            state.update(state='active', last_input=None, blocker=None, no_progress=state['no_progress'] + 1,
                         retry_after=new['not_before'], revision=state['revision'] + 1)
            store.put(db, 'agent_objective_planning', state)
        elif goal.get('stage_discovery'):
            from loom.autonomy.stages import planning_state

            objective = next(o for o in agent['role']['long_term_objectives'] if o['id'] == goal['long_term_objective_id'])
            state = planning_state(store, db, agent, objective)
            if state.get('discovery_id') != gid:
                raise ServiceError('Only the current baseline investigation can be retried', 409)
            state.update(discovery_id=new['id'], state='discovering', last_input=None, blocker=None, revision=state['revision'] + 1)
            store.put(db, 'agent_objective_planning', state)
        agent.update(generation=agent["generation"] + 1, next_wake_at=new["not_before"])
        store.put(db, "agents", agent)
        store.event(db, aid, "goal.retry_scheduled", dict(goal_id=new["id"], predecessor_id=gid, not_before=new["not_before"]))
        return store.receipt(db, body["command_id"], request, new)
