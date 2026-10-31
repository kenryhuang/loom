"""One durable, non-preemptive queue check per Agent; timer and manual entry share it."""

import threading
from datetime import timedelta

from loom.autonomy.contracts import scheduling_policy
from loom.autonomy.execution_budget import allocate, execution_block, expand_objective, parent_block, restore_budget_stage
from loom.autonomy.goals import sources_ready
from loom.autonomy.stages import role_signature, runnable
from loom.service.contracts import ServiceError, new_id


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
            try:
                self._cycle(aid)
            except Exception as exc:
                with self.store.sessions.transaction() as db:
                    current = self.store.get(db, "agents", aid)
                    if current.get("planner_lease"):
                        run = self.store.get(db, "agent_runs", current["planner_lease"], aid)
                        run.update(state="failed", reason=str(exc))
                        self.store.put(db, "agent_runs", run)
                        current["planner_lease"] = None
                    current.update(error=str(exc), activity="degraded")
                    current["scheduler"]["retry_generation"] = current["generation"]
                    self.store.put(db, "agents", current)
                self.finish(aid, "intervention", str(exc), steps=["检查原始错误并修正配置或输入，再点击手动调度。"])
            return True
        finally:
            lock.release()

    def finish(self, aid, state, reason, *, work=None, steps=None):
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", aid)
            agent["scheduler"].update(
                state=state, reason=reason, finished_at=self.store.now(), work_package_id=work, manual_required=state == "intervention", next_steps=steps or []
            )
            agent["activity"] = "degraded" if state == "intervention" else "working" if state in {"running", "dispatched", "recovered"} else "waiting"
            if state != "intervention":
                agent["error"] = None
            self.store.put(db, "agents", agent)
            self.store.event(db, aid, "schedule.finished", agent["scheduler"])

    def _cycle(self, aid):
        from loom.autonomy.control import drain

        drain(self.service, self.store, aid)
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
        if active:
            return self.finish(aid, "running", "当前任务仍在运行或等待执行槽位，本轮检查结束，不打断任务。", work=active[0])
        blocked = [w for w in works if w["state"] in {"blocked", "failed", "running", "dispatched"}]
        if blocked:
            work = blocked[0]
            try:
                self.recover(aid, work["id"])
                return self.finish(aid, "recovered", "已恢复原任务，等待执行；下一次定时检查继续核对状态。", work=work["id"])
            except ServiceError as exc:
                return self.finish(
                    aid,
                    "intervention",
                    str(exc),
                    work=work["id"],
                    steps=["查看此工作包的具体阻塞项和原 Session。", "按原因补充输入、恢复人工暂停或调整预算上限，再点击手动调度。"],
                )
        from loom.autonomy import goals, sources

        sources.refresh(self.store, aid)
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
        planning = next((p for p in latest["objective_planning"] if p["state"] in {"blocked", "awaiting_input", "deferred"}), None)
        if unresolved or planning:
            return self.finish(
                aid,
                "intervention",
                (planning or {}).get("reason") or "队列目标尚不可运行，请检查目标依赖、阶段和必需来源。",
                steps=["到目标页检查阶段的原始原因和待答问题，处理后手动调度。"],
            )
        self.finish(aid, "idle", "队列已完成，本轮现状检查没有产生新的可执行目标；按配置等待下次检查。")

    def dispatch_one(self, aid, works):
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", aid)
            pending_learning = [
                c for c in self.store.rows(db, "agent_learning_cycles", aid) if c["state"] not in {"completed", "failed", "cancelled", "incomplete"}
            ]
        if len(pending_learning) >= 20:
            return self.finish(aid, "intervention", "后台分析积压达到 20 项，暂不派发新工作。", steps=["处理后台分析的预算或失败项后手动调度。"])
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
                return self.finish(aid, "dispatched", "已按工作量分配预算并派发一个工作包，本次执行时间上限 1 小时。", work=work["id"])
        self.finish(
            aid,
            "intervention",
            "排队任务尚不能派发：请检查前序任务验收、来源时效、Role 版本或指定学习依赖。",
            steps=["在推进事项中查看具体缺失条件；刷新来源或修订合同后手动调度。"],
        )

    def recover(self, aid, wid):
        from loom.autonomy import sources

        sources.refresh(self.store, aid)
        store, service = self.store, self.service
        with service.lock:
            with store.sessions.transaction() as db:
                agent = store.get(db, "agents", aid)
                work = store.get(db, "agent_work_packages", wid, aid)
                if not work.get("session_id"):
                    raise ServiceError(work.get("blocked_reason") or "工作包合同不可派发，需修订或重新规划", 409)
                sid = work["session_id"]
                link = db.execute("SELECT * FROM agent_sessions WHERE session_id=? AND agent_id=?", (sid, aid)).fetchone()
                state = store.sessions._load(db, sid)
                run = state.get("run") or {}
                reason = run.get("reason") or (run.get("failure") or {}).get("message") or work.get("blocked_reason") or ""
                goal = store.get(db, "agent_goals", work["goal_id"], aid)
                block = execution_block(store, db, agent, work, state)
                if not link or link["taken_over"] or link["work_package_id"] not in {None, wid} or state.get("autonomy", {}).get("work_package_id") != wid:
                    raise ServiceError("Session 已被人工接管或用于其他任务，需手工处理归属", 409)
                if agent["state"] != "enabled" or sid in service.active or state["task"]["state"] not in {"paused", "failed"}:
                    raise ServiceError("Session 未处于可安全恢复状态，等待 worker 退出或处理恢复问题", 409)
                if state.get("workspace_blocked") or (state.get("input_request") or {}).get("state") == "pending":
                    raise ServiceError("Session 有待答问题或未确认副作用；请在原 Session 完成人工处理", 409)
                if db.execute(
                    "SELECT 1 FROM operations WHERE session_id=? AND json_extract(body,'$.status')='started' "
                    "AND json_extract(body,'$.effect_kind')='side_effecting' LIMIT 1",
                    (sid,),
                ).fetchone():
                    raise ServiceError("存在尚未确认结果的副作用操作，请在原 Session 核实后恢复", 409)
                if goal["state"] in {"paused", "cancelled", "superseded", "completed"}:
                    raise ServiceError("目标或阶段不可运行，请在目标页恢复或修订；执行原始原因：" + reason, 409)
                if not runnable(store, db, agent, goal) and not (block and restore_budget_stage(store, db, agent, goal)):
                    raise ServiceError("目标或阶段不可运行，请在目标页恢复或修订；执行原始原因：" + reason, 409)
                pinned = store.get(db, "agent_roles", f"{aid}:{state['autonomy']['role_revision']}", aid)["role"]
                if role_signature(pinned) != role_signature(agent["role"]) or goal.get("source_changed"):
                    raise ServiceError("执行合同或来源发生变化，需复核目标后恢复", 409)
                if not sources_ready(store, db, agent, goal):
                    raise ServiceError("已尝试刷新来源，但必需来源仍不可用；请修正路径或权限", 409)
                for lid in goal.get("learning_dependency", []):
                    cycle = store.get(db, "agent_learning_cycles", lid, aid)
                    if cycle["state"] != "completed" or cycle["coverage"] != "full":
                        raise ServiceError(f"指定分析 {lid} 尚未完整完成：{cycle['state']}/{cycle['coverage']}", 409)
                control = state.get("control") or {}
                legacy = (work.get("slice") or {}).get("pause_command_id") == control.get("command_id") and bool(control.get("command_id"))
                if control.get("command_id") and not legacy:
                    raise ServiceError("当前为人工暂停或停止；请手动恢复，调度器不会覆盖人工控制", 409)
                if not run.get("checkpoint") and not state.get("context"):
                    raise ServiceError("缺少持久 checkpoint 或恢复上下文，不能安全自动续跑", 409)
                policy = scheduling_policy(agent["role"])
                recovery = work.get("recovery", {})
                intent = recovery.get("pending")
                if not intent:
                    if recovery.get("attempts", 0) >= policy["max_recovery_attempts"]:
                        raise ServiceError(f"自动恢复次数已达 {recovery['attempts']}/{policy['max_recovery_attempts']}，需要手工介入", 409)
                    if block:
                        expand_objective(store, db, agent, goal, block)
                        if parent := parent_block(store, db, agent, work, block["requested"]):
                            raise ServiceError(parent + "；请调整对应上限或等待每日窗口。", 409)
                        scope = next((s for s in block["scopes"] if s["scope"] == "work"), None)
                        if scope:
                            allocate(store, db, agent, work, requested=scope["projected"])
                    elif reason in {"Token budget exceeded", "LLM token budget exceeded"}:
                        if parent := parent_block(store, db, agent, work, agent["role"]["budget"]["output_tokens"]):
                            raise ServiceError(parent + "；请调整对应上限或等待每日窗口。", 409)
                        allocate(store, db, agent, work, requested=state["task"]["limits"]["max_tokens"] + 1)
                    elif not legacy and reason != "Active time budget exceeded":
                        raise ServiceError(f"无法自动恢复：{reason or work.get('blocked_reason') or state['task']['state']}；请检查原 Session", 409)
                    elif parent := parent_block(store, db, agent, work, agent["role"]["budget"]["output_tokens"]):
                        raise ServiceError(parent + "；请调整对应上限或等待每日窗口。", 409)
                    intent = dict(id=new_id("schedule_resume"), run_id=run["id"], reason=reason)
                    work["recovery"] = dict(attempts=recovery.get("attempts", 0) + 1, pending=intent)
                    store.put(db, "agent_work_packages", work)
                    store.event(db, aid, "schedule.recovery.requested", dict(work_package_id=wid, **intent))
                if intent["run_id"] != run["id"]:
                    raise ServiceError("恢复意图与当前 run 不匹配，请检查执行历史", 409)
                state["task"]["limits"].update(
                    max_duration_seconds=3600, max_tokens=work.get("execution_budget", {}).get("tokens", state["task"]["limits"]["max_tokens"])
                )
                if run["state"] == "failed":
                    run["time_budget_start_seconds"] = run.get("active_seconds", 0)
                service.store.append(db, state, "agent.schedule.limits", dict(work_package_id=wid, **state["task"]["limits"]), command_id=intent["id"])
                work.update(state="blocked", blocked_reason=None)
                if work.get("slice"):
                    work["slice"]["phase"] = "retired"
                store.put(db, "agent_work_packages", work)
                if goal["state"] == "blocked":
                    goal.update(state="active", revision=goal["revision"] + 1)
                    store.put(db, "agent_goals", goal)
                db.execute("UPDATE agent_sessions SET work_package_id=? WHERE session_id=?", (wid, sid))
                service.store._save(db, state)
            from loom.autonomy.context import build_snapshot

            build_snapshot(store, aid, binding=state["autonomy"], session_id=sid, run_id=run["id"], purpose="schedule_resume")
            service.submit(sid, dict(command_id=intent["id"], type="resume", payload={}))
            with store.sessions.transaction() as db:
                work = store.get(db, "agent_work_packages", wid, aid)
                work["recovery"]["pending"] = None
                store.put(db, "agent_work_packages", work)
