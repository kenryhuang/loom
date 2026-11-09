"""One durable, non-preemptive queue check per Agent; timer and manual entry share it."""

import threading
from datetime import timedelta

from loom.autonomy.blockers import Code, from_exception, instructions, make, manual_required
from loom.autonomy.contracts import scheduling_policy
from loom.autonomy.execution_budget import allocate, execution_block, parent_block, restore_budget_stage
from loom.autonomy.goals import sources_ready
from loom.autonomy.stages import role_signature, runnable
from loom.core.sqlite import is_busy
from loom.service.contracts import ServiceError, new_id


class _Decision(ServiceError):
    """Decision-level recovery failure. The transaction rolls back; the attempt
    count (or exhaustion marker) is persisted by recover() in a fresh transaction."""

    def __init__(self, message, *, exhausted=False, details=None):
        super().__init__(message, 409, code=Code.RECOVERY_EXHAUSTED if exhausted else Code.EXECUTION_FAILURE)
        self.exhausted = exhausted
        self.details = details or {}


class QueueScheduler:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.service, self.store = supervisor.service, supervisor.store
        self.locks, self.guard = {}, threading.Lock()

    def advance(self, aid, *, manual=False):
        with self.guard:
            lock = self.locks.setdefault(aid, threading.Lock())
        if not lock.acquire(blocking=False):
            return False
        try:
            if self.supervisor.stopped.is_set():
                return False
            with self.store.sessions.transaction() as db:
                from loom.autonomy.configuration import activate

                activate(self.store, db, aid)
                agent = self.store.get(db, "agents", aid)
                policy = scheduling_policy(agent["role"])
                requested = manual or agent.get("schedule_requested", False)
                if agent["state"] != "enabled" or not requested and (not policy["enabled"] or agent.get("next_schedule_at", "") > self.store.now()):
                    return False
                if not requested and agent.get("error") and agent.get("scheduler", {}).get("retry_generation") == agent["generation"]:
                    agent["next_schedule_at"] = (self.store.clock() + timedelta(seconds=policy["tick_seconds"])).isoformat()
                    self.store.put(db, "agents", agent)
                    return False  # A failed planning call needs changed input or an explicit manual retry.
                agent.update(
                    schedule_requested=False,
                    next_schedule_at=(self.store.clock() + timedelta(seconds=policy["tick_seconds"])).isoformat(),
                    scheduler=dict(id=new_id("schedule"), state="checking", trigger="manual" if requested else "timer", started_at=self.store.now()),
                )
                self.store.put(db, "agents", agent)
                self.store.event(db, aid, "schedule.started", agent["scheduler"])
                budget_cursor = db.execute("SELECT COALESCE(MAX(rowid),0) FROM agent_budget_calls WHERE agent_id=?", (aid,)).fetchone()[0]
            try:
                self._cycle(aid)
            except Exception as exc:
                with self.store.sessions.transaction() as db:
                    current = self.store.get(db, "agents", aid)
                    # Planner cleanup may already have released its lease. A
                    # reservation also proves a call could have been sent.
                    planning_call = db.execute(
                        "SELECT 1 FROM agent_budget_calls WHERE agent_id=? AND rowid>? "
                        "AND category IN ('planning','stage_planning') LIMIT 1", (aid, budget_cursor),
                    ).fetchone()
                    retry_storage = is_busy(exc) and not current.get("planner_lease") and not planning_call
                    if current.get("planner_lease"):
                        run = self.store.get(db, "agent_runs", current["planner_lease"], aid)
                        run.update(state="failed", reason=str(exc))
                        self.store.put(db, "agent_runs", run)
                        current["planner_lease"] = None
                    current.update(error=None if retry_storage else str(exc), activity="waiting" if retry_storage else "degraded")
                    if not retry_storage:
                        current["scheduler"]["retry_generation"] = current["generation"]
                    self.store.put(db, "agents", current)
                if retry_storage:
                    self.finish(aid, "waiting", "Database is busy; the next scheduled check will retry.")
                    return True
                self.finish(aid, "intervention", str(exc), steps=["Fix the original error in the configuration or input, then run a manual check."],
                            blockers=[from_exception(exc, aid, None, None)])
            return True
        finally:
            lock.release()

    def finish(self, aid, state, reason, *, work=None, steps=None, blockers=None):
        if state == "intervention" and not blockers:
            blockers = [make(Code.EXECUTION_FAILURE, work or aid, reason, phase="queue")]
        if blockers:
            steps = [step for blocker in blockers for step in instructions(blocker)]
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", aid)
            agent["scheduler"].update(
                state=state, reason=reason, finished_at=self.store.now(), work_package_id=work, manual_required=state == "intervention",
                next_steps=steps or [], blockers=blockers or []
            )
            agent["activity"] = "degraded" if state == "intervention" else "working" if state in {"running", "dispatched", "recovered"} else "waiting"
            if state != "intervention":
                agent["error"] = None
            self.store.put(db, "agents", agent)
            self.store.event(db, aid, "schedule.finished", agent["scheduler"])

    def _cycle(self, aid):
        with self.store.sessions.read() as db:
            unified = self.store.get(db, "agents", aid).get("planning_engine") == 2
        if unified:
            return self.supervisor.unified.cycle(self, aid)
        from loom.autonomy.control import drain
        from loom.autonomy.sources import reconcile_own_changes

        drain(self.service, self.store, aid)
        reconcile_own_changes(self.store, aid)
        self.supervisor.stages.reconcile(aid)
        agent = self.store.snapshot(aid)
        replaced = {g.get("predecessor_id") for g in agent["goals"] if g.get("predecessor_id")}
        retired = replaced | {g["id"] for g in agent["goals"] if g["state"] in {"cancelled", "superseded"}}
        works = [
            w
            for w in agent["work_packages"]
            if not w.get("superseded_contract") and w["goal_id"] not in retired and w["state"] not in {"succeeded", "cancelled"}
        ]
        # Observe actual workers before any planning, recovery or dispatch.
        with self.service.lock, self.store.sessions.transaction() as db:
            active = []
            capability_exiting = False
            for work in works:
                pending = work.get("recovery", {}).get("pending")
                if pending and db.execute("SELECT 1 FROM commands WHERE id=?", (pending["id"],)).fetchone():
                    work["recovery"]["pending"] = None
                    self.store.put(db, "agent_work_packages", work)
                if not work.get("session_id"):
                    continue
                state = self.store.sessions._load(db, work["session_id"])
                if (state.get("autonomy") or {}).get("work_package_id") != work["id"]:
                    continue
                if work["session_id"] in self.service.active or state["task"]["state"] in {"running", "queued", "pausing"}:
                    active.append(work["id"])
                    capability_exiting |= (state['task']['state'] == 'paused'
                                           and (state.get('run') or {}).get('stop_code') == 'ACCEPTANCE_CAPABILITY_UNAVAILABLE')
        if active:
            self.finish(aid, "running", "A task is still running or waiting for an execution slot; this check ends without interrupting it.", work=active[0])
            if capability_exiting:
                with self.store.sessions.transaction() as db:
                    current = self.store.get(db, 'agents', aid)
                    current['next_schedule_at'] = (self.store.clock() + timedelta(seconds=1)).isoformat()
                    self.store.put(db, 'agents', current)
            return
        blocked = [
            w for w in works if w["state"] in {"blocked", "failed", "running", "dispatched"} and not w.get("recovery", {}).get("exhausted")
        ]
        if blocked:
            work = blocked[0]
            try:
                self.recover(aid, work["id"])
                return self.finish(aid, "recovered", "The original task was recovered and is waiting to run; the next scheduled check re-verifies its state.", work=work["id"])
            except ServiceError as exc:
                blocker = from_exception(exc, work["id"], None, None)
                with self.store.sessions.transaction() as db:
                    current = self.store.get(db, "agent_work_packages", work["id"], aid)
                    current["blocker"] = blocker
                    self.store.put(db, "agent_work_packages", current)
                return self.finish(
                    aid,
                    "intervention" if manual_required(blocker) else "waiting",
                    str(exc),
                    work=work["id"],
                    blockers=[blocker],
                    steps=["See this work package's specific blocker and original Session.", "Add input, resume the manual pause, or raise the budget cap as needed, then run a manual check."],
                )
        from loom.autonomy import goals, sources

        if agent.get('pending_configuration'):
            return self.finish(aid, 'waiting', 'The new configuration is saved and takes effect on the next check after dispatched tasks and analyses finish.')

        sources.refresh(self.store, aid)
        self.supervisor.stages.reconcile(aid)
        if works:
            return self.dispatch_one(aid, works)
        # Existing queued goals belong to this queue. Do not invent replacements for blocked goals.
        remaining = [g for g in agent["goals"] if g["state"] not in {"completed", "cancelled", "superseded"} and g["id"] not in replaced]
        if not remaining:
            from loom.autonomy.habits import materialize as habits
            from loom.autonomy.triggers import materialize

            materialize(self.store, aid)
            habits(self.store, aid)
            goals.materialize(self.store, aid)
            with self.store.sessions.transaction() as db:
                latest = self.store.get(db, "agents", aid)
                latest["scheduler"]["state"] = "planning"
                self.store.put(db, "agents", latest)
            self.supervisor.stages.advance(aid)
        latest = self.store.snapshot(aid)
        self.supervisor._plan(latest)
        latest = self.store.snapshot(aid)
        pending = [w for w in latest["work_packages"] if w["state"] == "pending" and not w.get("superseded_contract")]
        if pending:
            return self.dispatch_one(aid, pending)
        unresolved = [g for g in latest["goals"] if g["state"] not in {"completed", "cancelled", "superseded"} and g["id"] not in replaced]
        planning = [p for p in latest["objective_planning"] if p["state"] in {"blocked", "awaiting_input", "deferred"}]
        blockers = [p.get("blocker") or make(Code.LEGACY_UNCLASSIFIED, p["id"], p.get("reason") or p["state"]) for p in planning]
        if unresolved:
            selection = {s["goal_id"]: s for s in latest.get("selection", [])}
            blockers.extend(selection.get(g["id"], {}).get("blocker") or make(Code.GOAL_STATE, g["id"], "Goal is not runnable")
                            for g in unresolved)
        if blockers:
            intervention = any(manual_required(b) for b in blockers)
            return self.finish(
                aid,
                "intervention" if intervention else "waiting",
                (planning[0].get("reason") if planning else None) or "Queued goals are not runnable yet; check goal dependencies, stages, and required sources.",
                steps=["Resolve the corresponding blockers with the structured recovery actions before scheduling."] if intervention else ["Automatically rechecked after the budget window or quota changes."],
                blockers=blockers,
            )
        self.finish(aid, "idle", "The queue is complete; this check produced no new runnable goals. Waiting for the next check as configured.")

    def dispatch_one(self, aid, works):
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", aid)
            pending_learning = [
                c for c in self.store.rows(db, "agent_learning_cycles", aid) if c["state"] not in {"completed", "failed", "cancelled", "incomplete"}
            ]
        if len(pending_learning) >= 20:
            return self.finish(aid, "intervention", "Background analysis backlog reached 20 items; no new work will be dispatched.", steps=["Resolve the background analysis budget or failures, then run a manual check."])
        for work in sorted(works, key=lambda w: (w.get("created_at", ""), w.get("order", 0))):
            if work["state"] != "pending":
                continue
            with self.store.sessions.transaction() as db:
                current = self.store.get(db, "agent_work_packages", work["id"], aid)
                allocate(self.store, db, agent, current)
            self.supervisor.dispatcher.dispatch(aid, work["id"])
            with self.store.sessions.transaction() as db:
                current = self.store.get(db, "agent_work_packages", work["id"], aid)
            if current["state"] == "dispatched":
                return self.finish(aid, "dispatched", "Dispatched one work package on the shared daily token budget; this run is limited to 1 hour.", work=work["id"])
        self.finish(
            aid,
            "intervention",
            "Queued tasks cannot be dispatched yet: check prerequisite acceptance, source freshness, Role revision, or required learning dependencies.",
            steps=["See the missing conditions under Work; refresh sources or revise the contract, then run a manual check."],
        )

    def recover(self, aid, wid):
        from loom.autonomy import sources

        sources.refresh(self.store, aid)
        self.supervisor.stages.reconcile(aid)
        store, service = self.store, self.service
        try:
            with service.lock, store.sessions.transaction() as db:
                agent = store.get(db, "agents", aid)
                work = store.get(db, "agent_work_packages", wid, aid)
                if not work.get("session_id"):
                    raise ServiceError(work.get("blocked_reason") or "The work package contract cannot be dispatched; revise or replan it", 409, code=Code.EXECUTION_CONTRACT)
                sid = work["session_id"]
                link = db.execute("SELECT * FROM agent_sessions WHERE session_id=? AND agent_id=?", (sid, aid)).fetchone()
                state = store.sessions._load(db, sid)
                run = state.get("run") or {}
                reason = run.get("reason") or (run.get("failure") or {}).get("message") or work.get("blocked_reason") or ""
                goal = store.get(db, "agent_goals", work["goal_id"], aid)
                block = execution_block(store, db, agent, work, state)
                if not link or link["taken_over"] or link["work_package_id"] not in {None, wid} or state.get("autonomy", {}).get("work_package_id") != wid:
                    raise ServiceError("Session was taken over or is used by another task; resolve ownership manually", 409, code=Code.SESSION_OWNERSHIP)
                if agent["state"] != "enabled" or sid in service.active or state["task"]["state"] not in {"paused", "failed"}:
                    raise ServiceError("Session is not in a safely recoverable state; wait for the worker to exit or resolve the recovery issue", 409, code=Code.EXECUTION_BUSY)
                if state.get("workspace_blocked") or (state.get("input_request") or {}).get("state") == "pending":
                    raise ServiceError("Session has a pending question or unconfirmed side effect; finish the manual handling in the original Session", 409, code=Code.EXECUTION_INPUT)
                if db.execute(
                    "SELECT 1 FROM operations WHERE session_id=? AND json_extract(body,'$.status')='started' "
                    "AND json_extract(body,'$.effect_kind')='side_effecting' LIMIT 1",
                    (sid,),
                ).fetchone():
                    raise ServiceError("A side-effecting operation has no confirmed result; verify it in the original Session before resuming", 409, code=Code.EXECUTION_INPUT)
                if goal["state"] in {"paused", "cancelled", "superseded", "completed"}:
                    raise ServiceError("Goal or stage is not runnable; resume or revise it on the Goals tab. Original execution reason: " + reason, 409, code=Code.EXECUTION_CONTRACT)
                if not runnable(store, db, agent, goal) and not (block and restore_budget_stage(store, db, agent, goal)):
                    raise ServiceError("Goal or stage is not runnable; resume or revise it on the Goals tab. Original execution reason: " + reason, 409, code=Code.EXECUTION_CONTRACT)
                pinned = store.get(db, "agent_roles", f"{aid}:{state['autonomy']['role_revision']}", aid)["role"]
                if role_signature(pinned) != role_signature(agent["role"]) or goal.get("source_changed"):
                    raise ServiceError("The execution contract or a source changed; review the goal before resuming", 409, code=Code.EXECUTION_CONTRACT)
                if not sources_ready(store, db, agent, goal):
                    raise ServiceError("Sources were refreshed, but a required source is still unavailable; fix its path or permissions", 409, code=Code.SOURCE_UNAVAILABLE)
                for lid in goal.get("learning_dependency", []):
                    cycle = store.get(db, "agent_learning_cycles", lid, aid)
                    if cycle["state"] != "completed" or cycle["coverage"] != "full":
                        raise ServiceError(f"Required analysis {lid} is not fully complete: {cycle['state']}/{cycle['coverage']}", 409, code=Code.LEARNING_DEPENDENCY)
                control = state.get("control") or {}
                legacy = (work.get("slice") or {}).get("pause_command_id") == control.get("command_id") and bool(control.get("command_id"))
                if control.get("command_id") and not legacy:
                    raise ServiceError("The Session is under a manual pause or stop; resume it manually - the scheduler never overrides human control", 409, code=Code.MANUAL_PAUSE)
                # Acceptance repair restarts with a repaired spec instead of
                # resuming the old run, so the checkpoint premise only guards
                # the resume paths.
                from loom.service.failures import failure_details
                from loom.tasks.recovery import assess

                recovery_report = assess(state, failure=failure_details(db, state))
                acceptance_repair = recovery_report is not None and recovery_report['mode'] == 'reassemble'
                if recovery_report and recovery_report['mode'] == 'manual':
                    raise ServiceError("Recovery needs external dependencies or unknown execution results handled first", 409, code=Code.EXECUTION_INPUT)
                if not acceptance_repair and not run.get("checkpoint") and not state.get("context"):
                    raise ServiceError("Missing a persistent checkpoint or recovery context; cannot safely auto-resume", 409, code=Code.EXECUTION_CHECKPOINT)
                policy = scheduling_policy(agent["role"])
                recovery = work.get("recovery", {})
                intent = recovery.get("pending")
                repair = None
                if not intent:
                    # Decision-level failures consume one attempt (persisted by
                    # the handler below after this transaction rolls back);
                    # the premise failures above never do.
                    attempt_limit = max(policy['max_recovery_attempts'], recovery.get('attempt_limit', 0))
                    if recovery.get("attempts", 0) >= attempt_limit:
                        raise _Decision(
                            f"Auto-recovery attempts reached {recovery.get('attempts', 0)}/{attempt_limit}; manual intervention required", exhausted=True
                        )
                    if block:
                        if parent := parent_block(store, db, agent, work, block["requested"]):
                            raise parent
                    elif (run.get("stop_code") == "TOKEN_BUDGET_EXCEEDED" or (run.get("failure") or {}).get("code") == "TOKEN_BUDGET_EXCEEDED"
                          or any(c['code'] == 'TOKEN_BUDGET' for c in (recovery_report or {}).get('causes', []))):
                        if parent := parent_block(store, db, agent, work, agent["role"]["budget"]["output_tokens"]):
                            raise parent
                        allocate(store, db, agent, work, requested=(state['task']['limits'].get('max_tokens') or 0) + 1)
                    if acceptance_repair:
                        # Acceptance repair is two-phase: the recommendation is an
                        # LLM call and must run outside the service lock. Phase 1
                        # only captures the inputs; nothing is mutated here.
                        if parent := parent_block(store, db, agent, work, agent['role']['budget']['output_tokens']):
                            raise parent
                        from copy import deepcopy

                        repair = dict(state=deepcopy(state), report=recovery_report,
                                      generation=agent['generation'], run_id=run['id'], goal_revision=goal['revision'], reason=reason)
                    elif (not block and run.get('stop_code') != 'TOKEN_BUDGET_EXCEEDED'
                          and (run.get('failure') or {}).get('code') != 'TOKEN_BUDGET_EXCEEDED'
                          and not legacy and run.get("stop_code") != "ACTIVE_TIME_BUDGET_EXCEEDED"
                          and not (recovery_report and recovery_report['mode'] == 'resume')):
                        raise _Decision(f"Cannot auto-recover: {reason or work.get('blocked_reason') or state['task']['state']}; check the original Session")
                    elif not block and (parent := parent_block(store, db, agent, work, agent["role"]["budget"]["output_tokens"])):
                        raise parent
                    if repair is None:
                        intent = dict(id=new_id("schedule_resume"), run_id=run["id"], reason=reason, report=recovery_report)
                        work["recovery"] = dict(recovery, attempts=recovery.get("attempts", 0) + 1, pending=intent)
                        store.put(db, "agent_work_packages", work)
                        store.event(db, aid, "schedule.recovery.requested", dict(work_package_id=wid, **intent))
                if repair is None:
                    if intent["run_id"] != run["id"]:
                        raise ServiceError("The recovery intent does not match the current run; check the execution history", 409)
                    state["task"]["limits"].update(
                        max_duration_seconds=max(3600, state["task"]["limits"]["max_duration_seconds"]), max_tokens=None
                    )
                    if run["state"] == "failed":
                        run["time_budget_start_seconds"] = run.get("active_seconds", 0)
                    service.store.append(db, state, "agent.schedule.limits", dict(work_package_id=wid, **state["task"]["limits"]), command_id=intent["id"])
                    work.update(state="blocked", blocked_reason=None, blocker=None)
                    if work.get("slice"):
                        work["slice"]["phase"] = "retired"
                    store.put(db, "agent_work_packages", work)
                    if goal["state"] == "blocked":
                        goal.update(state="active", revision=goal["revision"] + 1)
                        store.put(db, "agent_goals", goal)
                    db.execute("UPDATE agent_sessions SET work_package_id=? WHERE session_id=?", (wid, sid))
                    service.store._save(db, state)
            if repair is not None:
                intent = self._repair(aid, wid, sid, repair)
                if intent.get('kind') == 'plan_revision':
                    return
            with service.lock:
                if intent.get("kind") in {"restart", "extend_tools"}:
                    from loom.tasks.recovery import assess

                    latest_state = service.store.snapshot(sid)
                    latest_report = assess(latest_state)
                    if ((latest_state.get('control') or {}).get('command_id') or not latest_report
                            or latest_report['source_key'] != intent['report']['source_key']):
                        raise ServiceError('State changed before the reassembly commit; keeping the current Session', 409, code=Code.EXECUTION_CONTRACT)
                    service.submit(
                        sid,
                        dict(command_id=intent["id"], type="resume" if intent["kind"] == "extend_tools" else "restart_task",
                             expected_task_revision=intent['task_revision'],
                             payload={} if intent["kind"] == "extend_tools" else
                             {"task": intent.get("task") or {"task_spec": intent["task_spec"]}, "reason": intent.get("reason") or ""}),
                    )
                else:
                    from loom.autonomy.context import build_snapshot

                    build_snapshot(store, aid, binding=state["autonomy"], session_id=sid, run_id=run["id"], purpose="schedule_resume")
                    service.submit(sid, dict(command_id=intent["id"], type="resume", payload={}))
                with store.sessions.transaction() as db:
                    work = store.get(db, "agent_work_packages", wid, aid)
                    work["recovery"]["pending"] = None
                    store.put(db, "agent_work_packages", work)
        except _Decision as decision:
            with service.lock, store.sessions.transaction() as db:
                work = store.get(db, "agent_work_packages", wid, aid)
                if decision.exhausted:
                    if not work.get("recovery", {}).get("exhausted"):
                        work["blocked_reason"] = "recovery_exhausted"
                        work.setdefault("recovery", {})["exhausted"] = True
                        store.put(db, "agent_work_packages", work)
                        store.event(db, aid, "schedule.recovery.exhausted", dict(work_package_id=wid, attempts=(work.get("recovery") or {}).get("attempts", 0)))
                else:
                    recovery = work.get("recovery", {})
                    work["recovery"] = dict(recovery, attempts=recovery.get("attempts", 0) + 1, pending=None, last_failure=decision.details)
                    store.put(db, "agent_work_packages", work)
            raise
        except ServiceError:
            # Cancel only an unsubmitted intent. An accepted command remains replayable.
            with service.lock, store.sessions.transaction() as db:
                current = store.get(db, 'agent_work_packages', wid, aid)
                pending = (current.get('recovery') or {}).get('pending')
                if pending and not db.execute('SELECT 1 FROM commands WHERE id=?', (pending['id'],)).fetchone():
                    current['recovery']['pending'] = None
                    store.put(db, 'agent_work_packages', current)
                    if current.get('session_id'):
                        session = store.sessions._load(db, current['session_id'])
                        if (session.get('pending_reassembly') or {}).get('command_id') == pending['id']:
                            session.pop('pending_reassembly', None)
                            store.sessions._save(db, session)
                    store.event(db, aid, 'schedule.recovery.invalidated', dict(work_package_id=wid, command_id=pending['id']))
            raise

    def _repair(self, aid, wid, sid, repair):
        """Prepare outside locks, then compare the exact inputs before committing."""
        with self.store.sessions.read() as db:
            work = self.store.get(db, "agent_work_packages", wid, aid)
        if work.get("engine_version") == 2:
            from loom.autonomy.planning import wake
            from loom.autonomy.stages import planning_state
            from loom.tasks.recovery import assess

            with self.service.lock, self.store.sessions.transaction() as db:
                agent = self.store.get(db, "agents", aid)
                work = self.store.get(db, "agent_work_packages", wid, aid)
                goal = self.store.get(db, 'agent_goals', work['goal_id'], aid)
                session = self.store.sessions._load(db, sid)
                from loom.service.failures import failure_details

                current_report = assess(session, failure=failure_details(db, session))
                if (agent['generation'] != repair['generation'] or (session.get('run') or {}).get('id') != repair['run_id']
                        or session['task']['state'] not in {'paused', 'failed'} or not current_report
                        or current_report['source_key'] != repair['report']['source_key']
                        or goal['revision'] != repair['goal_revision'] or goal.get('source_changed')
                        or agent['state'] != 'enabled' or not runnable(self.store, db, agent, goal)
                        or not sources_ready(self.store, db, agent, goal) or work.get('superseded_contract')):
                    raise ServiceError('The contract or execution changed during recovery', 409, code=Code.EXECUTION_CONTRACT)
                link = db.execute('SELECT * FROM agent_sessions WHERE session_id=? AND agent_id=?', (sid, aid)).fetchone()
                if (not link or link['taken_over'] or link['work_package_id'] not in {None, wid}
                        or session.get('autonomy', {}).get('work_package_id') != wid):
                    raise ServiceError('Session ownership changed during recovery', 409, code=Code.SESSION_OWNERSHIP)
                if (sid in self.service.active or session.get('workspace_blocked')
                        or (session.get('input_request') or {}).get('state') == 'pending'
                        or db.execute("SELECT 1 FROM operations WHERE session_id=? AND json_extract(body,'$.status')='started' "
                                      "AND json_extract(body,'$.effect_kind')='side_effecting'", (sid,)).fetchone()):
                    raise ServiceError('Resolve pending execution results before revising', 409, code=Code.EXECUTION_INPUT)
                from loom.autonomy.acceptance import objective_view

                objective = objective_view(agent, next(o for o in agent["role"]["long_term_objectives"] if o["id"] == work["objective_id"]))
                state = planning_state(self.store, db, agent, objective)
                fingerprint = repair['report']['source_key']
                if fingerprint in state.get('repair_fingerprints', []):
                    raise ServiceError("This execution gap already requested a plan revision; provide new input", 409, code=Code.EXECUTION_CONTRACT)
                if state['no_progress'] >= objective['stage_planning']['max_replans_without_progress']:
                    raise ServiceError("Plan revision limit reached; provide new input", 409, code=Code.RECOVERY_EXHAUSTED)
                state.setdefault('repair_fingerprints', []).append(fingerprint)
                state['answers'].append(dict(type='execution_gap', producer='host', report=repair['report']))
                state.update(revision_requested=True, last_input=None, next_review_at='', no_progress=state['no_progress'] + 1,
                             revision=state['revision'] + 1)
                self.store.put(db, 'agent_objective_planning', state)
                wake(self.store, agent)
                self.store.put(db, 'agents', agent)
            return dict(kind='plan_revision')
        from loom.autonomy.reassembly import build
        from loom.tasks.recovery import assess

        store, service = self.store, self.service
        try:
            prepared = build(service, repair['state'], repair['report'])
        except Exception as exc:
            if isinstance(exc, ServiceError) and exc.code == 'AGENT_BUDGET':
                # The reviewer can reserve more than the preliminary estimate.
                # A denied call is a budget premise, not a failed execution try.
                with store.sessions.transaction() as db:
                    work = store.get(db, 'agent_work_packages', wid, aid)
                    work.setdefault('recovery', {})['last_failure'] = dict(error_code='AGENT_BUDGET', recovery=repair['report'])
                    store.put(db, 'agent_work_packages', work)
                denial = ServiceError(str(exc), 409, code=Code.EXECUTION_BUDGET)
                denial.details = getattr(exc, 'budget_block', None) or {}
                raise denial from exc
            raise _Decision(f"Failed to prepare the recovery configuration: {exc}", details=dict(recovery=repair['report'], error_code=getattr(exc, 'code', 'REASSEMBLY_FAILED'))) from exc
        with service.lock, store.sessions.transaction() as db:
            agent = store.get(db, 'agents', aid)
            work = store.get(db, 'agent_work_packages', wid, aid)
            goal = store.get(db, 'agent_goals', work['goal_id'], aid)
            state = store.sessions._load(db, sid)
            run = state.get('run') or {}
            current_report = assess(state)
            if (agent['generation'] != repair['generation'] or run.get('id') != repair['run_id']
                    or state['task']['state'] not in {'paused', 'failed'} or not current_report
                    or current_report['source_key'] != repair['report']['source_key']
                    or goal['revision'] != repair['goal_revision'] or goal.get('source_changed')
                    or agent['state'] != 'enabled' or goal['state'] in {'paused', 'cancelled', 'superseded', 'completed'}
                    or not runnable(store, db, agent, goal) or not sources_ready(store, db, agent, goal)):
                raise ServiceError('The contract or state changed during recovery preparation; this configuration was not applied', 409, code=Code.EXECUTION_CONTRACT)
            link = db.execute('SELECT * FROM agent_sessions WHERE session_id=? AND agent_id=?', (sid, aid)).fetchone()
            if (not link or link['taken_over'] or link['work_package_id'] not in {None, wid}
                    or state.get('autonomy', {}).get('work_package_id') != wid):
                raise ServiceError('Session ownership changed during recovery', 409, code=Code.SESSION_OWNERSHIP)
            if sid in service.active or state.get('workspace_blocked'):
                raise ServiceError('Session still has an active execution; waiting for it to exit', 409, code=Code.EXECUTION_BUSY)
            if (state.get('control') or {}).get('command_id'):
                raise ServiceError('Session was taken under manual control during recovery', 409, code=Code.MANUAL_PAUSE)
            if ((state.get('input_request') or {}).get('state') == 'pending' or db.execute(
                    "SELECT 1 FROM operations WHERE session_id=? AND json_extract(body,'$.status')='started' "
                    "AND json_extract(body,'$.effect_kind')='side_effecting' LIMIT 1", (sid,)).fetchone()):
                raise ServiceError('There are unconfirmed results that must be verified first', 409, code=Code.EXECUTION_INPUT)
            recovery = work.get('recovery') or {}
            from loom.tasks.workspace_probe import digest

            if prepared.get('kind') != 'extend_tools':
                prepared['task']['limits'] = dict(state['task']['limits'], max_duration_seconds=3600,
                    max_tokens=None)
            prepared['report']['result_key'] = digest(prepared['task'])
            intent = dict(id=new_id('schedule_resume'), run_id=run['id'], reason=repair['reason'] or 'Structured task recovery',
                          kind=prepared.get('kind', 'restart'), task=prepared['task'], report=prepared['report'], task_revision=state['task']['revision'])
            context = prepared['context']
            context['command_id'] = intent['id']
            context['source_run_id'] = run['id']
            context['contract_key'] = state['autonomy'].get('acceptance_contract')
            context['task_key'] = prepared['report']['result_key']
            state['pending_reassembly'] = context
            store.sessions._save(db, state)
            work['recovery'] = {**recovery, 'attempts': recovery.get('attempts', 0) + 1, 'pending': intent,
                                'history': [*recovery.get('history', []), intent]}
            work.update(state='blocked', blocked_reason=None, blocker=None)
            store.put(db, 'agent_work_packages', work)
            store.event(db, aid, 'schedule.recovery.requested', dict(work_package_id=wid, **intent))
            if goal['state'] == 'blocked':
                goal.update(state='active', revision=goal['revision'] + 1)
                store.put(db, 'agent_goals', goal)
            db.execute('UPDATE agent_sessions SET work_package_id=? WHERE session_id=?', (wid, sid))
        return intent
