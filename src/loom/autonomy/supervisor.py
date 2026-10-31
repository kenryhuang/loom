"""Bounded persistent orchestration; Session workers retain execution ownership."""

import json
import threading
from datetime import timedelta

from loom.autonomy.dispatch import Dispatcher
from loom.autonomy.learning import LearningCoordinator
from loom.autonomy.planner import AgentPlanner
from loom.service.contracts import canonical, new_id


class AgentSupervisor:
    def __init__(self, service, store, background, *, planner=None):
        self.service, self.store = service, store
        self.planner = AgentPlanner(service, store, planner)
        self.dispatcher = Dispatcher(service, store)
        self.learning = LearningCoordinator(store, background)
        from loom.autonomy.stage_planner import StagePlanner
        from loom.autonomy.stages import StageCoordinator

        self.stages = StageCoordinator(service, store, StagePlanner(service, store, getattr(service, "agent_stage_planner", None)))
        from loom.autonomy.scheduling import QueueScheduler

        self.scheduler = QueueScheduler(self)
        self.timer_thread = None
        self.schedule_threads = {}
        self.stopped = threading.Event()
        self.thread = None
        self.tick_lock = threading.Lock()
        self.last_error = None
        # Expired process leases may be reclaimed; committed outbox remains authoritative.
        with store.sessions.transaction() as db:
            from loom.autonomy.results import record_session_state

            for row in db.execute("SELECT session_id FROM agent_sessions").fetchall():
                try:
                    state = store.sessions._load(db, row[0])
                except Exception:
                    continue
                record_session_state(db, state)
            for row in db.execute("SELECT body FROM agents").fetchall():
                agent = json.loads(row[0])
                if agent.get("planner_lease"):
                    agent.update(planner_lease=None, generation=agent["generation"] + 1)
                    store.put(db, "agents", agent)
                if agent.get("scheduler", {}).get("state") in {"checking", "planning"}:
                    agent["scheduler"].update(state="interrupted", reason="Service restart; next check reconciles actual Session state")
                    agent.update(next_schedule_at=store.now(), schedule_requested=True)
                    store.put(db, "agents", agent)
            for row in db.execute("SELECT body FROM agent_runs").fetchall():
                run = json.loads(row[0])
                if run["state"] == "planning":
                    run.update(state="recoverable" if run.get("kind") in {"stage_plan", "stage_review"} else "superseded", reason="Service restart")
                    store.put(db, "agent_runs", run)

    def start(self):
        if self.thread is None and not self.stopped.is_set():
            self.thread = threading.Thread(target=self._loop, name="loom-agent-supervisor", daemon=True)
            self.thread.start()
            self.timer_thread = threading.Thread(target=self._timer_loop, name="loom-agent-background", daemon=True)
            self.timer_thread.start()

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.timer_thread:
            self.timer_thread.join(timeout=5)
        for thread in list(self.schedule_threads.values()):
            thread.join(timeout=5)

    def _timer_loop(self):
        # Independent of model planning latency; controls still use service admission.
        while not self.stopped.wait(0.5):
            with self.store.sessions.transaction() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM agents")]
            for aid in ids:
                try:
                    self.learning.advance(aid, manual=False)
                    from loom.autonomy import profile
                    from loom.autonomy.method_feedback import advance as methods

                    profile.scan_feedback(self.store, aid)
                    profile.review_due(self.store, aid)
                    methods(self.store, self.learning.background, aid)
                    with self.store.sessions.transaction() as db:
                        agent = self.store.get(db, "agents", aid)
                        if agent.pop("background_error", None):
                            self.store.put(db, "agents", agent)
                except Exception as exc:
                    with self.store.sessions.transaction() as db:
                        agent = self.store.get(db, "agents", aid)
                        if agent.get("background_error") != str(exc):
                            agent["background_error"] = str(exc)
                            self.store.put(db, "agents", agent)
                            self.store.event(db, aid, "background.error", dict(reason=str(exc)))

    def _loop(self):
        while not self.stopped.wait(0.5):
            try:
                from loom.autonomy.contracts import scheduling_policy

                with self.store.sessions.transaction() as db:
                    agents = [json.loads(r[0]) for r in db.execute("SELECT body FROM agents")]
                for agent in agents:
                    aid = agent["id"]
                    if self.stopped.is_set():
                        break
                    if agent["state"] != "enabled" or (self.schedule_threads.get(aid) and self.schedule_threads[aid].is_alive()):
                        continue
                    policy = scheduling_policy(agent["role"])
                    if not agent.get("schedule_requested") and (not policy["enabled"] or agent.get("next_schedule_at", "") > self.store.now()):
                        continue
                    thread = threading.Thread(target=self.scheduler.advance, args=(aid,), name=f"loom-schedule-{aid}", daemon=True)
                    self.schedule_threads[aid] = thread
                    thread.start()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            with self.store.sessions.transaction() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM agents ORDER BY COALESCE(json_extract(body,'$.last_served_at'),'')")]
            for aid in ids:
                if self.stopped.is_set():
                    break
                try:
                    self.scheduler.advance(aid)
                except Exception as exc:
                    with self.store.sessions.transaction() as db:
                        agent = self.store.get(db, "agents", aid)
                        if agent.get("planner_lease"):
                            run = self.store.get(db, "agent_runs", agent["planner_lease"], aid)
                            run.update(state="failed", reason=str(exc))
                            self.store.put(db, "agent_runs", run)
                        agent.update(
                            activity="degraded", error=str(exc), planner_lease=None, next_wake_at=(self.store.clock() + timedelta(seconds=60)).isoformat()
                        )
                        self.store.put(db, "agents", agent)
                        self.store.event(db, aid, "agent.error", {"message": str(exc)})
        finally:
            self.tick_lock.release()

    def _agent(self, aid):
        # Explicit internal check, shared with the timer/manual scheduler logic.
        return self.scheduler.advance(aid, manual=True)

    def _plan(self, agent):
        aid = agent["id"]
        with self.store.sessions.transaction() as db:
            current = self.store.get(db, "agents", aid)
            if current["generation"] != agent["generation"] or current.get("planner_lease"):
                return
            goals = self.store.rows(db, "agent_goals", aid)
            packages = self.store.rows(db, "agent_work_packages", aid)
            from loom.autonomy.goals import rank

            candidates, decisions = rank(self.store, db, current, goals, packages)
            goal = candidates[0] if candidates else None
            run = dict(
                id=new_id("agent_run"),
                agent_id=aid,
                state="planning" if goal else "no_action",
                generation=agent["generation"],
                role_revision=agent["role_revision"],
                created_at=self.store.now(),
                goal_id=goal["id"] if goal else None,
                reason="Ready goal" if goal else "No new ready goal",
                selection=decisions,
            )
            current.update(
                last_served_at=self.store.now(),
                selection=decisions,
                planner_lease=run["id"] if goal else None,
                next_wake_at=(self.store.clock() + timedelta(seconds=agent["role"]["interval_seconds"])).isoformat(),
                activity="planning" if goal else ("working" if any(w["state"] in {"dispatched", "running"} for w in packages) else "waiting"),
            )
            self.store.put(db, "agents", current)
            self.store.put(db, "agent_runs", run)
            if not goal:
                self.store.event(db, aid, "planning.no_action", {"agent_run_id": run["id"], "reason": run["reason"]})
                return
        from loom.autonomy.context import build_snapshot

        context = build_snapshot(self.store, aid, goal=goal, purpose="planning")
        proposal = self.planner.propose(agent, goal, context)
        with self.store.sessions.transaction() as db:
            latest = self.store.get(db, "agents", aid)
            latest_goal = self.store.get(db, "agent_goals", goal["id"], aid)
            if (
                self.stopped.is_set()
                or latest["state"] != "enabled"
                or latest["generation"] != agent["generation"]
                or latest_goal["revision"] != goal["revision"]
            ):
                run.update(state="superseded", reason="Planning inputs changed")
            else:
                refs = []
                for order, item in enumerate(proposal):
                    wid = new_id("work")
                    work = dict(
                        **item,
                        id=wid,
                        agent_id=aid,
                        goal_id=goal["id"],
                        goal_revision=goal["revision"],
                        agent_run_id=run["id"],
                        role_revision=agent["role_revision"],
                        state="pending",
                        order=order,
                        created_at=self.store.now(),
                        run_ids=[],
                        read_only=goal.get("read_only", False),
                        stage_id=goal.get("stage_id"),
                        roadmap_id=goal.get("roadmap_id"),
                        objective_id=goal["long_term_objective_id"],
                    )
                    self.store.put(db, "agent_work_packages", work)
                    db.execute("INSERT INTO agent_outbox VALUES(?,?,?,?,?)", (wid, aid, "dispatch", "pending", canonical(work)))
                    refs.append(wid)
                run.update(state="committed", work_package_ids=refs, context_ref=context["id"])
                latest_goal.update(state="active", revision=latest_goal["revision"] + 1)
                self.store.put(db, "agent_goals", latest_goal)
                latest.update(activity="working", error=None)
            if latest.get("planner_lease") == run["id"]:
                latest["planner_lease"] = None
            self.store.put(db, "agents", latest)
            self.store.put(db, "agent_runs", run)
            self.store.event(db, aid, "planning.finished", {"agent_run_id": run["id"], "state": run["state"]})
