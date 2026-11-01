"""Session-compatible routing and replayable dispatch, under service admission."""

from loom.autonomy.binding import bind_for_dispatch, compatible_config
from loom.memory.store import bound_workspace, tokens
from loom.service.contracts import ServiceError


def task_payload(agent, work, binding=None):
    role = agent["role"]
    text = work["objective"] + "\nAcceptance criteria:\n" + "\n".join("- " + c for c in work["acceptance"])
    payload = dict(
        objective=text,
        workspace=role["working_directory"],
        model=role["model"],
        plan_mode=role.get("plan_mode") or "auto",
        knowledge_base_ids=role["knowledge_base_ids"],
        external_tool_ids=role["external_tool_ids"],
        **({"task_spec": role["task_spec"]} if role["task_spec"] else {}),
        limits={"max_tokens": work.get("execution_budget", {}).get("tokens", role["budget"]["work_tokens"]), "max_duration_seconds": 3600},
    )
    if binding is not None:
        # Normalize so the reuse-compatibility comparison and session creation
        # see the same effective configuration.
        from loom.tasks.assembly import normalize_task_spec

        payload["plan_mode"] = binding["plan_mode"]
        spec = normalize_task_spec(binding.get("task_spec"), workspace=role["working_directory"], plan_mode=binding["plan_mode"])
        environment = spec.setdefault("session_environment", {"plugin": "session"})
        if role["working_directory"] and not any(r.get("kind") == "directory" for r in environment.get("resources", [])):
            environment["resources"] = [
                *environment.get("resources", []),
                {"id": "workspace", "kind": "directory", "uri": role["working_directory"], "access": "read-write"},
            ]
        payload["task_spec"] = spec
    if work.get('read_only'):
        payload['external_tool_ids'] = []
        payload['task_spec'] = dict(session_environment=dict(plugin='session', resources=[
            dict(id='workspace', kind='directory', uri=role['working_directory'], access='read-only')]
            if role['working_directory'] else []), tools=dict(collections=['filesystem', 'task_control']
            if role['working_directory'] else ['task_control']))
    return payload


class Dispatcher:
    def __init__(self, service, store, *, fault=None):
        self.service, self.store = service, store
        self.fault = fault or (lambda point: None)

    def _quiet_block(self, db, aid, agent, work, goal):
        """Non-mutating dispatch gates, evaluated before the binding recommendation
        (phase 1) and again under the service lock (phase 2). Returns a reason when
        the work package currently cannot dispatch."""
        from loom.autonomy.stages import runnable

        if not runnable(self.store, db, agent, goal):
            return "goal_not_runnable"
        if goal["state"] in {"paused", "cancelled", "superseded", "blocked"}:
            return "goal_" + goal["state"]
        siblings = self.store.rows(db, "agent_work_packages", aid)
        if any(
            w["state"] in {"dispatched", "running", "blocked"}
            and w.get("blocked_reason") != "recovery_exhausted"
            and w["id"] != work["id"]
            and w.get("session_id")
            for w in siblings
        ):
            return "sibling_active"
        if any(
            w["goal_id"] == goal["id"] and not w.get("superseded_contract") and w["order"] < work["order"] and w["state"] != "succeeded"
            for w in siblings
        ):
            return "earlier_unsucceeded"
        for lid in goal.get("learning_dependency", []):
            cycle = self.store.get(db, "agent_learning_cycles", lid, aid)
            if cycle["state"] != "completed" or cycle["coverage"] != "full":
                return "learning_incomplete"
        return None

    def dispatch(self, aid, wid):
        # Phase 1: lock-free snapshot. The recommendation calls an LLM and must
        # never hold the service lock, and never runs when a gate already blocks
        # the dispatch; every premise is re-validated in phase 2 and bind()
        # re-checks generation, so a stale snapshot cannot apply.
        from loom.autonomy.goals import sources_ready

        try:
            with self.store.sessions.transaction() as db:
                agent = self.store.get(db, "agents", aid)
                work = self.store.get(db, "agent_work_packages", wid, aid)
                if agent["state"] != "enabled" or work["state"] != "pending":
                    return
                goal = self.store.get(db, "agent_goals", work["goal_id"], aid)
                if self._quiet_block(db, aid, agent, work, goal):
                    return
                # The marking gates still run (and mutate) in phase 2 only; their
                # condition already tells phase 1 the recommendation would be wasted.
                stale = (
                    bool(goal.get("source_changed"))
                    or not sources_ready(self.store, db, agent, goal)
                    or work["role_revision"] != agent["role_revision"]
                )
        except ServiceError:
            return
        dynamic = None if stale else bind_for_dispatch(self.service, agent, work)
        payload = task_payload(agent, work, binding=dynamic)
        # Lock order is service lock -> SessionStore lock throughout dispatch/control.
        with self.service.lock:
            with self.store.sessions.transaction() as db:
                agent = self.store.get(db, "agents", aid)
                work = self.store.get(db, "agent_work_packages", wid, aid)
                if agent["state"] != "enabled" or work["state"] != "pending":
                    return
                goal = self.store.get(db, "agent_goals", work["goal_id"], aid)
                if self._quiet_block(db, aid, agent, work, goal):
                    return
                if goal.get('source_changed'):
                    work['blocked_reason'] = 'Source changed after planning; review or revise the goal before dispatch'
                    self.store.put(db, 'agent_work_packages', work)
                    return
                if not sources_ready(self.store, db, agent, goal):
                    work['blocked_reason'] = 'Required current source must be refreshed before dispatch'
                    self.store.put(db, 'agent_work_packages', work)
                    return
                if work["role_revision"] != agent["role_revision"]:
                    work.update(state="blocked", blocked_reason="role_changed; revise or replace work contract")
                    self.store.put(db, "agent_work_packages", work)
                    return
                candidates = []
                for link in db.execute("SELECT * FROM agent_sessions WHERE agent_id=?", (aid,)):
                    reason = None
                    try:
                        state = self.store.sessions._load(db, link["session_id"])
                    except ServiceError:
                        continue
                    if link["taken_over"] or link["work_package_id"]:
                        reason = "taken_over_or_reserved"
                    elif work.get('read_only'):
                        reason = 'Routine checks require a fresh restricted environment'
                    elif state["session_id"] in self.service.active or state["task"]["state"] != "idle" or state.get("workspace_blocked"):
                        reason = "busy_or_recovering"
                    elif state.get("input_request") and state["input_request"]["state"] == "pending":
                        reason = "pending_input"
                    elif any(m["role"] == "user" and m["state"] == "accepted" for m in state["messages"]):
                        reason = "pending_message"
                    elif (
                        bound_workspace(state["task"]) != agent["role"]["working_directory"]
                        or state["task"].get("model") != agent["role"]["model"]
                        or state["task"].get("knowledge_base_ids", []) != agent["role"]["knowledge_base_ids"]
                        or state["task"].get("external_tool_ids", []) != agent["role"]["external_tool_ids"]
                        or state.get("autonomy")
                        and state["autonomy"]["role_revision"] != agent["role_revision"]
                        or dynamic is not None
                        and not compatible_config(state["task"], payload)
                    ):
                        reason = "incompatible_configuration"
                    score = len(tokens(state["task"]["objective"]) & tokens(work["objective"]))
                    candidates.append(dict(session_id=state["session_id"], reason=reason, score=score, revision=state["task"]["revision"]))
                valid = sorted([c for c in candidates if c["reason"] is None and c["score"]], key=lambda c: c["score"], reverse=True)
                selected = valid[0] if valid else None
                route = dict(
                    id="route_" + wid,
                    agent_id=aid,
                    work_package_id=wid,
                    action="reuse" if selected else "create",
                    candidates=candidates,
                    selected=selected,
                    reason="Compatible related context" if selected else "No compatible related idle Session",
                )
                self.store.put(db, "agent_routes", route)
            self.fault("before_dispatch")
            cid = "agent-dispatch:" + wid

            def bind(state, emit, db):
                current = self.store.get(db, "agents", aid)
                current_work = self.store.get(db, "agent_work_packages", wid, aid)
                if current["state"] != "enabled" or current["generation"] != agent["generation"] or current_work["state"] != "pending":
                    raise ServiceError("Dispatch invalidated by newer Agent state", 409)
                sid = state["session_id"]
                link = db.execute("SELECT * FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
                if link and (link["agent_id"] != aid or link["taken_over"] or link["work_package_id"]):
                    raise ServiceError("Session ownership changed", 409)
                binding = dict(
                    agent_id=aid,
                    goal_id=work["goal_id"],
                    work_package_id=wid,
                    role_revision=agent["role_revision"],
                    output_tokens=agent["role"]["budget"]["output_tokens"],
                    previous_run_id=(state.get("run") or {}).get("id"),
                    objective_id=goal["long_term_objective_id"],
                    stage_id=goal.get("stage_id"),
                    roadmap_id=goal.get("roadmap_id"),
                )
                state["autonomy"] = binding
                state["task"]["objective"] = payload["objective"]
                state["task"]["goal_revision"] += 1
                state["task"]["limits"].update(payload["limits"])
                db.execute(
                    "INSERT INTO agent_sessions VALUES(?,?,?,0) ON CONFLICT(session_id) DO UPDATE SET work_package_id=excluded.work_package_id", (sid, aid, wid)
                )
                if dynamic is not None:
                    work["binding"] = dynamic["record"]
                work.update(state="dispatched", session_id=sid, dispatch_command_id=cid)
                self.store.put(db, "agent_work_packages", work)
                db.execute("UPDATE agent_outbox SET state='delivered' WHERE id=?", (wid,))
                self.store.event(db, aid, "work.dispatched", dict(work_package_id=wid, session_id=sid, route=route["action"]))
                if dynamic is not None:
                    record = dynamic["record"]
                    self.store.event(db, aid, "schedule.binding." + record["source"],
                                     dict(work_package_id=wid, **{k: v for k, v in record.items() if k != "source"}))
                emit("agent.work.bound", binding)

            if selected:
                self.store.sessions.submit(
                    selected["session_id"],
                    dict(command_id=cid, type="submit_message", expected_task_revision=selected["revision"], payload={"content": payload["objective"]}),
                    after=bind,
                    producer="agent_planner",
                )
            else:
                self.store.sessions.create(cid, payload, after=bind, producer="agent_planner")
            self.fault("after_dispatch")
