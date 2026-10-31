"""Persistent per-terminal-run coordination of fixed-source Deep evaluations."""

import json
import threading
from copy import deepcopy
from datetime import timedelta

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.contracts import learning_policy, scheduling_policy
from loom.autonomy.learning_budget import admit_resume, current_block, progress
from loom.autonomy.learning_queue import TERMINAL, ordered, reconcile
from loom.service.contracts import ServiceError, new_id


def _budget_messages(value):
    if isinstance(value, str):
        return [value] if "Agent " in value and "budget exhausted" in value else []
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return list(dict.fromkeys(message for child in children for message in _budget_messages(child)))


class LearningCoordinator:
    def __init__(self, store, background):
        self.store, self.background = store, background
        self.lock = threading.Lock()

    def advance(self, aid, *, manual=True):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.store.sessions.transaction() as db:
                agent = self.store.get(db, "agents", aid)
                policy = scheduling_policy(agent["role"])
                requested = agent.get("learning_schedule_requested", False)
                if not manual and not requested and (not policy["enabled"] or agent.get("next_learning_at", "") > self.store.now()):
                    return
                agent.update(learning_schedule_requested=False,
                             next_learning_at=(self.store.clock() + timedelta(seconds=policy["tick_seconds"])).isoformat())
                self.store.put(db, "agents", agent)
            self._advance_cycles(aid)
        finally:
            self.lock.release()

    def _advance_cycles(self, aid):
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", aid)
            cycles = reconcile(self.store, db, aid)
            has_jobs = db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_jobs'").fetchone()
            active_ids = set()
            for cycle in cycles:
                if cycle["state"] == "failed":
                    cycle.update(state="incomplete", auto_resume=False, retry_after="")
                    self.store.put(db, "agent_learning_cycles", cycle)
                    db.execute("UPDATE agent_outbox SET state='incomplete' WHERE id=?", (cycle["id"],))
                    self.store.event(db, aid, "learning.migrated", dict(cycle_id=cycle["id"], state="incomplete"))
                if has_jobs and cycle.get("evaluation_id"):
                    active = db.execute("SELECT 1 FROM semantic_jobs WHERE id=? AND json_extract(body,'$.state') IN ('queued','running')",
                                        (cycle["evaluation_id"],)).fetchone()
                    if active:
                        active_ids.add(cycle["id"])
                        if cycle["state"] != "cancelled":
                            cycle.update(state="running", retry_after="")
                            self.store.put(db, "agent_learning_cycles", cycle)
        # Cancel controls are serviced even when a different job owns the head.
        cycles = sorted(ordered(cycles), key=lambda c: c.get("requested_action") != "cancel")
        for cycle in cycles:
            action = cycle.get("requested_action")
            if active_ids and cycle["id"] not in active_ids and action != "cancel":
                continue  # Includes a cancellation still draining its model call.
            # Reconsider legacy next-window deferrals once when the structured
            # budget policy is first installed, without requiring manual Resume.
            migrate_budget = cycle.get("auto_resume") and not cycle.get("budget_policy_seen")
            migrate_partial = cycle["state"] == "completed" and cycle.get("coverage") == "partial" and cycle.get("evaluation_id")
            if not action and (
                agent["state"] != "enabled"
                or cycle["state"] in TERMINAL
                and not migrate_partial
            ):
                continue
            if cycle.get("retry_after", "") > self.store.now() and not (migrate_budget or action):
                return  # Deferred head retains its position until the bounded retry.
            if action == "retry" and agent["state"] != "enabled":
                continue
            before = deepcopy(cycle)
            if not cycle.get("completion_policy_seen") and cycle.get("evaluation_id"):
                acknowledged = [r for r in cycle.get("budget_resumes", []) if r["id"] != (cycle.get("budget_resume_pending") or {}).get("id")]
                cycle["attempts"] = max(cycle.get("attempts", 1), 1 + len(acknowledged))
            cycle["budget_policy_seen"] = 1
            cycle["completion_policy_seen"] = 1
            if migrate_partial:
                cycle["state"] = "pending"
            try:
                if action == "cancel":
                    if cycle.get("evaluation_id"):
                        self.background.semantic.cancel(cycle["session_id"], cycle["analysis_id"], cycle["evaluation_id"])
                    cycle.update(state="cancelled", requested_action=None)
                else:
                    if action == "retry":
                        self._resume(cycle)
                        cycle.update(state="pending", requested_action=None, blocked_reason=None, retry_after="")
                    self._advance(agent, cycle)
            except ServiceError as exc:
                # Deleting the original evidence must never silently recreate it.
                cycle["control_failures"] = cycle.get("control_failures", 0) + 1
                cycle.update(
                    state="incomplete"
                    if exc.status == 404 or exc.code.startswith("LEARNING_") or cycle["control_failures"] >= learning_policy(agent["role"])["max_attempts"]
                    else "deferred",
                    blocked_reason=str(exc),
                    retry_after=(self.store.clock() + timedelta(seconds=60)).isoformat(),
                )
            except (ValueError, OSError) as exc:
                cycle["control_failures"] = cycle.get("control_failures", 0) + 1
                cycle.update(
                    state="incomplete" if cycle["control_failures"] >= learning_policy(agent["role"])["max_attempts"] else "deferred",
                    blocked_reason=str(exc),
                    retry_after=(self.store.clock() + timedelta(seconds=60)).isoformat(),
                )
            if cycle["state"] in TERMINAL:
                cycle.update(auto_resume=False, retry_after="", requested_action=None)
            if cycle != before:
                with self.store.sessions.transaction() as db:
                    current = self.store.get(db, "agent_learning_cycles", cycle["id"], aid)
                    if current.get("command_generation", 0) != before.get("command_generation", 0):
                        continue
                    self.store.put(db, "agent_learning_cycles", cycle)
                    db.execute("UPDATE agent_outbox SET state=? WHERE id=?", (cycle["state"], cycle["id"]))
                    self.store.event(db, aid, "learning.updated", {"cycle_id": cycle["id"], "state": cycle["state"], "coverage": cycle["coverage"]})
            # Exactly one head is checked. An unchanged running evaluation still
            # owns the queue; completion/abandonment permits the next timer tick.
            return

    def _resume(self, cycle, *, retry_failed=True):
        if not cycle.get("evaluation_id"):
            return
        job = self.background.semantic._load(cycle["session_id"], cycle["analysis_id"], cycle["evaluation_id"])
        if job["state"] in {"queued", "running"}:
            return  # A lost acknowledgement is reconciled from the same evaluation.
        public = self.background.semantic._public(job)
        options = {"resume_id": cycle["evaluation_id"], **cycle.get("retry_options", {})}
        if retry_failed and public.get("retryable_work"):
            options["retry_failed"] = True
        kwargs = {}
        if pending := cycle.get("budget_resume_pending"):
            with self.store.sessions.transaction() as db:
                current = self.store.get(db, "agent_learning_cycles", cycle["id"], cycle["agent_id"])
                agent = self.store.get(db, "agents", cycle["agent_id"])
            if agent["state"] != "enabled" or current.get("command_generation", 0) != cycle.get("command_generation", 0):
                raise ServiceError("Learning control changed before budget resume", 409)
            kwargs["resume_request_id"] = pending["id"]
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", cycle["agent_id"])
            current = self.store.get(db, "agent_learning_cycles", cycle["id"], cycle["agent_id"])
            if agent["state"] != "enabled" or current.get("command_generation", 0) != cycle.get("command_generation", 0):
                raise ServiceError("Learning control changed before resume", 409)
            policy = learning_policy(agent["role"])
            intent = current.get("resume_intent")
            if not intent or intent["id"] in job.get("host_resume_requests", []):
                attempts = max(cycle.get("attempts", 1), current.get("attempts", 1))
                round_attempts = attempts - cycle.get("round_start_attempts", 0)
                if round_attempts >= policy["max_attempts"]:
                    raise ServiceError(
                        f"Analysis attempt limit reached: {round_attempts}/{policy['max_attempts']}; accepted memories retained", 409, "LEARNING_RETRY_LIMIT"
                    )
                intent = dict(id=kwargs.get("resume_request_id") or new_id("learning_resume"), options=options, attempt=attempts + 1)
                current.update(attempts=intent["attempt"], resume_intent=intent)
                self.store.put(db, "agent_learning_cycles", current)
                self.store.event(db, cycle["agent_id"], "learning.attempt.scheduled", dict(cycle_id=cycle["id"], **intent))
            cycle.update(attempts=intent["attempt"], resume_intent=intent)
        kwargs = {"resume_request_id": intent["id"]}
        if not job.get("autonomy_budget") and all(cycle.get(k) for k in ("goal_id", "work_package_id")):
            kwargs["budget_context"] = dict(agent_id=agent["id"], goal_id=cycle["goal_id"], work_package_id=cycle["work_package_id"],
                                           output_tokens=agent["role"]["budget"]["output_tokens"])
        self.background.semantic.start(cycle["session_id"], cycle["analysis_id"], intent["options"], **kwargs)

    def _advance(self, agent, cycle):
        sid = cycle["session_id"]
        if not cycle.get("analysis_id"):
            analysis = self.background.trajectory.start(sid, source_cursor=cycle["terminal_event_cursor"])
            cycle.update(analysis_id=analysis["id"], state="running", phase="factual_analysis")
        analysis = self.background.trajectory.get(sid, cycle["analysis_id"])
        if analysis["state"] in {"queued", "running"}:
            cycle.update(state="running", phase="factual_analysis")
            return
        if analysis["state"] != "completed":
            cycle["analysis_attempts"] = cycle.get("analysis_attempts", 0) + 1
            if cycle["analysis_attempts"] >= learning_policy(agent["role"])["max_attempts"]:
                raise ServiceError(
                    "Factual analysis retry limit reached; inspect the source analysis error: " + str(analysis.get("error")), 409, "LEARNING_RETRY_LIMIT"
                )
            self.background.trajectory.start(sid, source_cursor=cycle["terminal_event_cursor"])
            raise ServiceError(analysis.get("error", "Factual analysis incomplete"), 409)
        if not cycle.get("evaluation_id"):
            listing = self.background.semantic.list(sid, cycle["analysis_id"])
            runs = listing["run_ids"]
            if cycle["run_id"] not in runs:
                cycle.update(state="completed", coverage="not_applicable", blocked_reason="No analyzable run evidence")
                return
            options = dict(
                analysis_version="goal_graph_v1",
                run_id=cycle["run_id"],
                extract_memory=True,
                max_calls=min(48, learning_policy(agent["role"])["max_evaluation_calls"]),
            )
            if agent["role"]["evaluation_model"]:
                options["model"] = agent["role"]["evaluation_model"]
            binding = dict(
                agent_id=agent["id"], goal_id=cycle["goal_id"], work_package_id=cycle["work_package_id"], output_tokens=agent["role"]["budget"]["output_tokens"]
            )
            # Persist initial settings before admission; a restart or lost reply
            # must not create another history after a Role edit.
            options = cycle.setdefault("initial_options", options)
            with self.store.sessions.transaction() as db:
                current = self.store.get(db, "agent_learning_cycles", cycle["id"], agent["id"])
                if current.get("command_generation", 0) != cycle.get("command_generation", 0):
                    raise ServiceError("Learning control changed before start", 409)
                self.store.put(db, "agent_learning_cycles", cycle)
            evaluation = next((j for j in listing.get("jobs", []) if j.get("run_id") == cycle["run_id"]
                               and j.get("autonomy_budget", {}).get("agent_id") == agent["id"]
                               and j.get("settings", {}).get("extract_memory")), None)
            if not evaluation:
                evaluation = self.background.semantic.start(sid, cycle["analysis_id"], options, budget_context=binding)
            cycle.update(evaluation_id=evaluation["id"], phase="deep_eval", state="running", attempts=cycle["attempts"] + 1)
        job = self.background.semantic._load(sid, cycle["analysis_id"], cycle["evaluation_id"])
        records = job.get("checkpoint", {}).get("memory", {}).get("records", {})
        published = [r for r in records.values() if r.get("publication_state", "published") == "published" and r["kind"] in {"facts", "lessons", "workflows"}]
        cycle["memory_result"] = dict(accepted=len(published), evaluation_state=job["state"])
        if published and cycle.get("coverage") != "full":
            cycle["coverage"] = "partial"
        if job["state"] in {"queued", "running"}:
            cycle.update(state="running", phase="deep_eval")
            if cycle.get("budget_resume_pending"):
                cycle.update(state="running", budget_resume_pending=None, retry_after="")
            return
        policy = learning_policy(agent["role"])
        if (
            job["state"] != "completed"
            and cycle.get("attempts", 1) - cycle.get("round_start_attempts", 0) >= policy["max_attempts"]
            and not (cycle.get("resume_intent") and cycle["resume_intent"]["id"] not in job.get("host_resume_requests", []))
        ):
            raise ServiceError(
                f"Analysis attempt limit reached: {cycle['attempts'] - cycle.get('round_start_attempts', 0)}/{policy['max_attempts']}; "
                "accepted memories retained", 409, "LEARNING_RETRY_LIMIT"
            )
        pending = cycle.get("budget_resume_pending")
        if pending and (job["state"] == "completed" or pending["id"] in job.get("host_resume_requests", [])):
            cycle["budget_resume_pending"] = None
        if cycle.get("budget_resume_pending"):
            self._resume(cycle, retry_failed=False)
            cycle.update(state="running", auto_resume=False, blocked_reason=None, retry_after="", budget_resume_pending=None)
            return
        control_pause = any(
            e.get("provider_code") in {"AGENT_CONTROL", "AGENT_BUDGET"} and e.get("reason") == "Agent is paused; new calls are deferred"
            for e in job.get("error_details", [])
        )
        if control_pause and job["state"] != "completed":
            self._resume(cycle, retry_failed=False)
            cycle.update(state="running", blocked_reason=None, retry_after="")
            return  # Enabled Agent resumes the same checkpoint; a control event never expands budgets.
        block = current_block(self.store, job)
        if block and job["state"] != "completed":
            cycle["budget_policy_seen"] = 1
            decision = admit_resume(self.store, agent["id"], cycle, job, block)
            cycle["budget_decision"] = decision
            cycle["coverage"] = "partial" if published else "none"
            if decision["outcome"] == "resume":
                self._resume(cycle, retry_failed=False)
                cycle.update(state="running", auto_resume=False, blocked_reason=None, retry_after="", budget_resume_pending=None)
            elif decision["outcome"] == "wait":
                cycle["budget_waits"] = cycle.get("budget_waits", 0) + 1
                cycle.update(
                    state="incomplete" if cycle["budget_waits"] >= policy["max_attempts"] else "deferred",
                    auto_resume=True,
                    blocked_reason=decision["reason"],
                    retry_after=(self.store.clock() + timedelta(seconds=scheduling_policy(agent["role"])["tick_seconds"])).isoformat(),
                )
            else:
                cycle.update(
                    state="incomplete" if decision["outcome"] == "stop" else "deferred",
                    auto_resume=False,
                    blocked_reason=decision["reason"],
                    retry_after=(self.store.clock() + timedelta(seconds=60)).isoformat(),
                )
            return
        if cycle.get("auto_resume") and cycle.get("retry_after", "") > self.store.now():
            return  # Unsupported legacy errors retain their existing deferral.
        settings, usage = job.get("settings", {}), job.get("usage", {})
        increases = {}
        for key, used, ceiling in [("max_calls", usage.get("calls", 0), policy["max_evaluation_calls"]), ("max_seconds", job.get("elapsed_seconds", 0), 7200)]:
            limit = settings.get(key)
            if job["state"] != "completed" and limit and used >= limit:
                target = min(ceiling, max(limit * 2, int(used) + 1))
                if target <= used:
                    raise ServiceError(
                        f"Evaluation {key} ceiling reached: used={used}, limit={ceiling}; revise policy or narrow analysis", 409, "LEARNING_LIMIT"
                    )
                increases[key] = target
        if increases:
            cycle["retry_options"] = {**cycle.get("retry_options", {}), **increases}
            self._resume(cycle, retry_failed=False)
            cycle.update(state="running", blocked_reason=None, retry_after="")
            return
        errors = {k: job.get(k) for k in ("error", "error_details")}
        if not job.get("host_resume_requests") and not cycle.get("budget_resumes") and job["state"] == "incomplete":
            # Pre-policy jobs sometimes stored their only failure in a stage.
            # Once a host resume is recorded these are historical evidence.
            errors["checkpoint"] = job.get("checkpoint")
        failures = json.dumps(errors)
        budget_reason = " | ".join(_budget_messages(errors))
        if job["state"] == "completed" and not self.background.semantic._public(job).get("retryable_work"):
            failures = ""  # Historical errors in a resumed checkpoint are not current blockers.
        # Cumulative limits still block after midnight, even if a daily limit is
        # also exhausted. Keep recognizing persisted errors from older versions.
        if any(f"Agent {scope} token budget exhausted" in failures for scope in ("contract", "goal", "work")):
            cycle.update(
                state="incomplete",
                coverage="partial" if published else "none",
                blocked_reason=budget_reason or "Goal/work budget exhausted; revise the budget before retrying learning",
            )
        elif "Agent daily" in failures or "Agent learning token budget" in failures:
            if cycle.get("auto_resume") and cycle.get("budget_window_retry", "") <= self.store.now():
                self._resume(cycle)
                cycle.update(state="running", auto_resume=False, blocked_reason=None)
            else:
                cycle["budget_waits"] = cycle.get("budget_waits", 0) + 1
                cycle.update(
                    state="incomplete" if cycle["budget_waits"] >= policy["max_attempts"] else "deferred",
                    auto_resume=True,
                    coverage="partial" if published else "none",
                    blocked_reason=budget_reason or "Learning budget exhausted; resume the same evaluation in the next budget window",
                    budget_window_retry=BudgetLedger(self.store.sessions.path, clock=self.store.clock).next_window(agent["id"]),
                    retry_after=(self.store.clock() + timedelta(seconds=scheduling_policy(agent["role"])["tick_seconds"])).isoformat(),
                )
        elif job["state"] in {"completed", "incomplete"}:
            public = self.background.semantic._public(job)
            coverage = "partial" if job["state"] == "incomplete" or public.get("retryable_work") else "full"
            if coverage == "full":
                cycle.update(state="completed", phase="feedback", coverage=coverage, blocked_reason=None)
            elif public.get("retryable_work"):
                fingerprint = progress(job)
                if cycle.get("last_retry_progress") == fingerprint:
                    raise ServiceError(
                        "Repeated analysis produced no new completed units or published records; inspect rejected items before retrying",
                        409,
                        "LEARNING_NO_PROGRESS",
                    )
                cycle.update(last_retry_progress=fingerprint, coverage=coverage)
                self._resume(cycle)
                cycle.update(state="running", blocked_reason=None, retry_after="")
            else:
                cycle.update(
                    state="incomplete",
                    coverage=coverage,
                    blocked_reason=job.get("error") or "Analysis has non-retryable gaps; inspect rejected items and repair their evidence or protocol",
                )
        elif job["state"] == "interrupted":
            self._resume(cycle, retry_failed=False)
            cycle.update(state="running")
        else:
            cycle.update(state="incomplete", coverage="partial" if published else "none", blocked_reason=job.get("error", job["state"]))
