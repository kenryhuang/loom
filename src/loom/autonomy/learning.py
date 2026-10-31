"""Persistent per-terminal-run coordination of fixed-source Deep evaluations."""

import json
from copy import deepcopy
from datetime import timedelta

from loom.autonomy.budget import BudgetLedger
from loom.autonomy.learning_budget import admit_resume, current_block
from loom.service.contracts import ServiceError


def _budget_messages(value):
    if isinstance(value, str):
        return [value] if "Agent " in value and "budget exhausted" in value else []
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return list(dict.fromkeys(message for child in children for message in _budget_messages(child)))


class LearningCoordinator:
    def __init__(self, store, background):
        self.store, self.background = store, background

    def advance(self, aid):
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", aid)
            cycles = self.store.rows(db, "agent_learning_cycles", aid)
        for cycle in cycles:
            action = cycle.get("requested_action")
            # Reconsider legacy next-window deferrals once when the structured
            # budget policy is first installed, without requiring manual Resume.
            migrate_budget = cycle.get("auto_resume") and not cycle.get("budget_policy_seen")
            if not action and (
                agent["state"] != "enabled" or cycle["state"] in {"completed", "cancelled", "failed"}
                or cycle.get("retry_after", "") > self.store.now() and not migrate_budget
            ):
                continue
            if action == "retry" and agent["state"] != "enabled":
                continue
            before = deepcopy(cycle)
            cycle["budget_policy_seen"] = 1
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
                cycle.update(
                    state="failed" if exc.status == 404 else "deferred",
                    blocked_reason=str(exc),
                    retry_after=(self.store.clock() + timedelta(seconds=60)).isoformat(),
                )
            except (ValueError, OSError) as exc:
                cycle.update(state="deferred", blocked_reason=str(exc), retry_after=(self.store.clock() + timedelta(seconds=60)).isoformat())
            if cycle != before:
                with self.store.sessions.transaction() as db:
                    current = self.store.get(db, "agent_learning_cycles", cycle["id"], aid)
                    if current.get("command_generation", 0) != before.get("command_generation", 0):
                        continue
                    self.store.put(db, "agent_learning_cycles", cycle)
                    db.execute("UPDATE agent_outbox SET state=? WHERE id=?", (cycle["state"], cycle["id"]))
                    self.store.event(db, aid, "learning.updated", {"cycle_id": cycle["id"], "state": cycle["state"], "coverage": cycle["coverage"]})
            # Continue past running jobs so one slow evaluation cannot starve later facts.
            if cycle != before:
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
        self.background.semantic.start(cycle["session_id"], cycle["analysis_id"], options, **kwargs)

    def _advance(self, agent, cycle):
        sid = cycle["session_id"]
        if not cycle.get("analysis_id"):
            analysis = self.background.trajectory.start(sid, source_cursor=cycle["terminal_event_cursor"])
            cycle.update(analysis_id=analysis["id"], state="running", phase="factual_analysis")
        analysis = self.background.trajectory.get(sid, cycle["analysis_id"])
        if analysis["state"] in {"queued", "running"}:
            return
        if analysis["state"] != "completed":
            self.background.trajectory.start(sid, source_cursor=cycle["terminal_event_cursor"])
            raise ServiceError(analysis.get("error", "Factual analysis incomplete"), 409)
        if not cycle.get("evaluation_id"):
            runs = self.background.semantic.list(sid, cycle["analysis_id"])["run_ids"]
            if cycle["run_id"] not in runs:
                cycle.update(state="completed", coverage="not_applicable", blocked_reason="No analyzable run evidence")
                return
            options = dict(analysis_version="goal_graph_v1", run_id=cycle["run_id"], extract_memory=True)
            if agent["role"]["evaluation_model"]:
                options["model"] = agent["role"]["evaluation_model"]
            binding = dict(
                agent_id=agent["id"], goal_id=cycle["goal_id"], work_package_id=cycle["work_package_id"], output_tokens=agent["role"]["budget"]["output_tokens"]
            )
            evaluation = self.background.semantic.start(sid, cycle["analysis_id"], options, budget_context=binding)
            cycle.update(evaluation_id=evaluation["id"], phase="deep_eval", state="running", attempts=cycle["attempts"] + 1)
        job = self.background.semantic._load(sid, cycle["analysis_id"], cycle["evaluation_id"])
        records = job.get("checkpoint", {}).get("memory", {}).get("records", {})
        published = [r for r in records.values() if r.get("publication_state", "published") == "published" and r["kind"] in {"facts", "lessons", "workflows"}]
        cycle["memory_result"] = dict(accepted=len(published), evaluation_state=job["state"])
        if job["state"] in {"queued", "running"}:
            if cycle.get("budget_resume_pending"):
                cycle.update(state="running", budget_resume_pending=None, retry_after="")
            return
        pending = cycle.get("budget_resume_pending")
        if pending and (job["state"] == "completed" or pending["id"] in job.get("host_resume_requests", [])):
            cycle["budget_resume_pending"] = None
        if cycle.get("budget_resume_pending"):
            self._resume(cycle, retry_failed=False)
            cycle.update(state="running", auto_resume=False, blocked_reason=None, retry_after="", budget_resume_pending=None)
            return
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
                cycle.update(state="deferred", auto_resume=True, blocked_reason=decision["reason"],
                             retry_after=BudgetLedger(self.store.sessions.path, clock=self.store.clock).next_window(agent["id"]))
            else:
                cycle.update(state="failed" if decision["outcome"] == "stop" else "deferred", auto_resume=False,
                             blocked_reason=decision["reason"], retry_after=(self.store.clock() + timedelta(seconds=60)).isoformat())
            return
        if cycle.get("auto_resume") and cycle.get("retry_after", "") > self.store.now():
            return  # Unsupported legacy errors retain their existing deferral.
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
                state="failed",
                coverage="partial" if published else "none",
                blocked_reason=budget_reason or "Goal/work budget exhausted; revise the budget before retrying learning",
            )
        elif "Agent daily" in failures or "Agent learning token budget" in failures:
            if cycle.get("auto_resume"):
                self._resume(cycle)
                cycle.update(state="running", auto_resume=False, blocked_reason=None)
            else:
                cycle.update(
                    state="deferred",
                    auto_resume=True,
                    coverage="partial" if published else "none",
                    blocked_reason=budget_reason or "Learning budget exhausted; resume the same evaluation in the next budget window",
                    retry_after=BudgetLedger(self.store.sessions.path, clock=self.store.clock).next_window(agent["id"]),
                )
        elif job["state"] in {"completed", "incomplete"}:
            public = self.background.semantic._public(job)
            coverage = "partial" if job["state"] == "incomplete" or public.get("retryable_work") else "full"
            cycle.update(state="completed", phase="feedback", coverage=coverage)
        elif job["state"] == "interrupted":
            self._resume(cycle, retry_failed=False)
            cycle.update(state="running")
        else:
            cycle.update(state="failed", coverage="partial" if published else "none", blocked_reason=job.get("error", job["state"]))
