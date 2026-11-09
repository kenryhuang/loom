"""Unified objective controller: snapshots -> compiled revisions -> execution.

Legacy Agents retain their old controller until an explicit safe migration.
Only this controller publishes engine-2 plans; the queue never invents work.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from loom.autonomy.acceptance import objective_view
from loom.autonomy.blockers import Code, from_exception, make
from loom.autonomy.contracts import scheduling_policy, stage_enabled
from loom.autonomy.plan_compiler import capabilities, compile_plan, environments
from loom.autonomy.plan_evidence import PROTOCOL_VERSION
from loom.autonomy.plan_evidence import prepare as prepare_evidence
from loom.autonomy.plan_planner import PlanPlanner
from loom.autonomy.stage_overlap import delivery_index
from loom.autonomy.stages import TERMINAL, evidence, planning_state, role_signature
from loom.autonomy.store import digest
from loom.llm.structured import ProtocolError
from loom.service.contracts import ServiceError, canonical
from loom.tasks.workspace_probe import probe


def wake(store, agent):
    agent["next_wake_at"] = store.now()
    if scheduling_policy(agent["role"])["enabled"]:
        agent["next_schedule_at"] = store.now()


def input_key(agent, objective, state, stage, sources, executions, trigger=None):
    return digest(dict(engine=agent.get("planning_engine"), protocol_version=PROTOCOL_VERSION, role=role_signature(agent["role"]), objective=objective,
        stage=stage, answers=state.get("answers", []), required_revision=state.get("required_revision"),
        requested_goals=state.get("requested_goals", []), guidance_version=agent["guidance_version"],
        sources=sources, executions=executions, trigger=(trigger or {}).get("key")))


def execution_facts(store, db, aid, oid):
    goals = [g for g in store.rows(db, "agent_goals", aid) if g["long_term_objective_id"] == oid]
    ids = {g["id"] for g in goals}
    return dict(goals=[(g["id"], g["revision"], g["state"]) for g in goals],
                works=[(w["id"], w["state"], w.get("contract_digest"), w.get("run_ids", []))
                       for w in store.rows(db, "agent_work_packages", aid) if w["goal_id"] in ids])


def planning_facts(store, db, aid):
    return dict(sources=[(s["id"], s.get("digest"), s.get("state"), s.get("version")) for s in store.rows(db, "agent_sources", aid)],
                observations=store.rows(db, "agent_observations", aid),
                habits=[p for p in store.rows(db, "agent_profile", aid) if p.get("habit") and p.get("state") == "active"])


class ObjectiveCoordinator:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.service, self.store = supervisor.service, supervisor.store
        self.planner = PlanPlanner(self.service, self.store)

    def cycle(self, scheduler, aid):
        from loom.autonomy.configuration import activate
        from loom.autonomy.control import drain
        from loom.autonomy.sources import reconcile_own_changes, refresh

        drain(self.service, self.store, aid)
        reconcile_own_changes(self.store, aid)
        with self.service.lock, self.store.sessions.transaction() as db:
            activate(self.store, db, aid)
        refresh(self.store, aid)
        snapshot = self.store.snapshot(aid)
        works = [w for w in snapshot["work_packages"] if w.get("engine_version") == 2 and not w.get("superseded_contract")]
        active_work = None
        with self.service.lock, self.store.sessions.transaction() as db:
            for work in works:
                if not work.get("session_id"):
                    continue
                session = self.store.sessions._load(db, work["session_id"])
                if work["session_id"] in self.service.active or session["task"]["state"] in {"running", "queued", "pausing"}:
                    active_work = work["id"]
                    break
        if active_work:
            return scheduler.finish(aid, "running", "A compiled work package is running.", work=active_work)
        if snapshot.get("pending_configuration"):
            return scheduler.finish(aid, "waiting", "Waiting for a safe configuration boundary.")
        if self.reconcile_contracts(snapshot):
            snapshot = self.store.snapshot(aid)
        # A local gate does not starve independent objectives. All still share one execution slot.
        for objective in snapshot["role"]["long_term_objectives"]:
            with self.store.sessions.transaction() as db:
                agent = self.store.get(db, "agents", aid)
                state = planning_state(self.store, db, agent, objective)
                stage = self.store.get(db, "agent_stages", state["stage_id"], aid) if state.get("stage_id") else None
            if state.get("manual_pause") or state["state"] in {"cancelled", "completed", "awaiting_input"}:
                continue
            if not self.enabled(agent, objective, state, stage):
                continue
            current = [w for w in works if stage and w.get("plan_id") == stage.get("plan_id")]
            if stage and stage["state"] not in TERMINAL and not state.get("revision_requested"):
                for work in current:
                    if work["state"] == "pending":
                        try:
                            self.supervisor.dispatcher.dispatch(aid, work["id"])
                            with self.store.sessions.transaction() as db:
                                latest = self.store.get(db, "agent_work_packages", work["id"], aid)
                            if latest["state"] == "dispatched":
                                return scheduler.finish(aid, "dispatched", "Dispatched the next compiled work package.", work=work["id"])
                        except ServiceError as exc:
                            self.work_gate(aid, work, exc)
                    elif work["state"] in {"failed", "blocked", "dispatched", "running"}:
                        try:
                            scheduler.recover(aid, work["id"])
                            return scheduler.finish(aid, "recovered", "Resuming the original execution contract.", work=work["id"])
                        except ServiceError as exc:
                            self.work_gate(aid, work, exc)
                if current and not all(w["state"] == "succeeded" for w in current):
                    continue
            if self.advance(aid, objective["id"]):
                # Commit schedules another immediate bounded queue pass, not a long polling delay.
                return scheduler.finish(aid, "planned", "Planning decision recorded; the next queue pass advances the published plan.")
        latest = self.store.snapshot(aid)
        gates = [s.get("blocker") for s in latest["objective_planning"] if s.get("blocker")]
        gates += [w.get("blocker") for w in latest["work_packages"] if w.get("blocker") and not w.get("superseded_contract")]
        scheduler.finish(aid, "waiting" if gates else "idle", gates[0]["message"] if gates else "No runnable work; waiting for a relevant change.",
                         blockers=gates)

    def reconcile_contracts(self, snapshot):
        """Old published contracts use the same rules as a new proposal.

        A detected contract fault requests one owner-side revision. Its stable
        marker prevents every queue tick from buying another planning attempt.
        """
        from copy import deepcopy

        from loom.autonomy.binding import validated_assembly
        from loom.tasks.acceptance import AcceptanceController
        from loom.tasks.acceptance_contract import RULES_VERSION, InvalidAcceptanceContract

        changed = False
        for work in snapshot["work_packages"]:
            if (work.get("engine_version") != 2 or work.get("superseded_contract") or work["state"] in {"succeeded", "cancelled"}
                    or work["verification_plan"].get("rules_version", 0) >= RULES_VERSION):
                continue
            try:
                _, assembly = validated_assembly(self.service, work["compiled_payload"])
                controller = AcceptanceController(assembly)
                controller.data["plan"] = deepcopy(work["verification_plan"])
                controller.validate_retained_contract()
            except InvalidAcceptanceContract as exc:
                with self.service.lock, self.store.sessions.transaction() as db:
                    agent = self.store.get(db, "agents", snapshot["id"])
                    objective = next(o for o in agent["role"]["long_term_objectives"] if o["id"] == work["objective_id"])
                    state = planning_state(self.store, db, agent, objective)
                    stage = self.store.get(db, "agent_stages", state["stage_id"], agent["id"]) if state.get("stage_id") else None
                    latest_work = self.store.get(db, "agent_work_packages", work["id"], agent["id"])
                    marker = digest([work["id"], work["contract_digest"], RULES_VERSION])
                    if (agent["generation"] != snapshot["generation"] or state.get("manual_pause")
                            or not stage or stage["state"] != "active" or stage.get("plan_id") != work["plan_id"]
                            or latest_work["state"] in {"succeeded", "cancelled"} or latest_work.get("superseded_contract")
                            or latest_work["contract_digest"] != work["contract_digest"]
                            or marker in state.get("invalid_contracts", [])):
                        continue
                    state.setdefault("invalid_contracts", []).append(marker)
                    state["answers"].append(dict(type="contract_invalid", producer="host", work_package_id=work["id"],
                                                 criterion_id=exc.criterion_id, reason=str(exc), rules_version=RULES_VERSION))
                    state.update(revision_requested=True, state="ready", last_input=None, next_review_at="", blocker=None,
                                 reason=str(exc), revision=state["revision"] + 1)
                    self.store.put(db, "agent_objective_planning", state)
                    wake(self.store, agent)
                    self.store.put(db, "agents", agent)
                    self.store.event(db, agent["id"], "plan.contract.invalid", dict(work_package_id=work["id"], reason=str(exc)))
                    changed = True
            except (ServiceError, ValueError):
                continue  # Environment admission has its own capability/source gates.
        return changed

    def enabled(self, agent, objective, state, stage):
        if stage_enabled(objective):
            return True
        if stage and stage["state"] not in TERMINAL:
            return True
        trigger = self.trigger(agent, objective)
        return trigger is not None and trigger["key"] != state.get("last_trigger")

    def trigger(self, agent, objective, facts=None):
        elapsed = max(0, (self.store.clock() - datetime.fromisoformat(agent["created_at"])).total_seconds())
        schedule = objective.get("schedule")
        if schedule:
            return dict(key="period:" + str(int(elapsed // schedule["every_seconds"])), kind="periodic", contract=schedule)
        if facts is None:
            with self.store.sessions.read() as db:
                facts = planning_facts(self.store, db, agent["id"])
        habits = [p for p in facts["habits"] if p["habit"]["objective_id"] == objective["id"]]
        observations = [o for o in facts["observations"] if o["objective_id"] == objective["id"]]
        if habits:
            return dict(key=digest([(h["id"], h.get("revision"), int(elapsed // h["habit"]["every_seconds"])) for h in habits]),
                        kind="routine", habits=habits)
        iteration = objective.get("iteration")
        if iteration and observations:
            latest = {}
            for observation in observations:
                latest[(observation.get("indicator"), observation.get("object_id"))] = observation
            review = int(elapsed // iteration["review_seconds"])
            if any(o["state"] != "met" for o in latest.values()) or review:
                return dict(key=digest([list(latest.values()), review]), kind="observation", observations=list(latest.values()), contract=iteration)
        return None

    def work_gate(self, aid, work, exc):
        with self.store.sessions.transaction() as db:
            current = self.store.get(db, "agent_work_packages", work["id"], aid)
            blocker = from_exception(exc, work["id"], work.get("contract_digest"), None)
            blocker["owner_ref"] = dict(kind="work_package", id=work["id"], revision=work.get("contract_revision", 1))
            blocker["allowed_actions"] = [dict(type="replan", objective_id=work["objective_id"])]
            if current.get("blocker") != blocker:
                current.update(blocker=blocker, blocked_reason=str(exc))
                self.store.put(db, "agent_work_packages", current)
                self.store.event(db, aid, "plan.work.waiting", dict(work_package_id=work["id"], blocker=blocker))

    def advance(self, aid, oid):
        store = self.store
        with store.sessions.transaction() as db:
            agent = store.get(db, "agents", aid)
            objective = objective_view(agent, next(o for o in agent["role"]["long_term_objectives"] if o["id"] == oid))
            state = planning_state(store, db, agent, objective)
            if agent["state"] != "enabled" or agent.get("planner_lease") or state.get("manual_pause"):
                return False
            stage = store.get(db, "agent_stages", state["stage_id"], aid) if state.get("stage_id") else None
            stage = stage if stage and stage["state"] not in TERMINAL else None
            sources = planning_facts(store, db, aid)
            trigger = self.trigger(agent, objective, sources)
            key = input_key(agent, objective, state, stage, sources, execution_facts(store, db, aid, oid), trigger)
            budget_key = digest([agent["role"]["budget"]["daily_tokens"],
                                 store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()])
            budget_changed = state.get("budget_key") and state["budget_key"] != budget_key
            if (not budget_changed and state.get("last_input") == key
                    and (not state.get("next_review_at") or state["next_review_at"] > store.now())):
                return False
            goals = [g for g in store.rows(db, "agent_goals", aid) if g["long_term_objective_id"] == oid]
            history = [s for s in store.rows(db, "agent_stages", aid) if s["objective_id"] == oid]
            records = evidence(store, db, aid, goals)
            prior_attempts = []
            required_verification = []
            by_goal = {g["id"]: g for g in goals}
            for work in store.rows(db, "agent_work_packages", aid):
                if not stage or work.get("stage_id") != stage["id"] or work.get("superseded_contract"):
                    continue
                from loom.autonomy.acceptance import recorded_verdict

                goal = by_goal[work["goal_id"]]
                proven = (recorded_verdict(db, work) and goal.get("source_key") == digest(sources)
                          and goal.get("role_signature") == role_signature(agent["role"]))
                if not proven:
                    required_verification.extend(dict(work_package_id=work["id"], criterion=c)
                        for c in work.get("verification_plan", {}).get("criteria", []) if c["verifier"] != "semantic")
                if not work.get("session_id") or work["state"] == "succeeded":
                    continue
                row = db.execute("SELECT seq,json_extract(body,'$.payload.input.report') FROM events WHERE session_id=? "
                    "AND json_extract(body,'$.type')='tool.started' AND json_extract(body,'$.payload.tool_id')='finish' "
                    "ORDER BY seq DESC LIMIT 1", (work["session_id"],)).fetchone()
                if row and isinstance(row[1], str):
                    prior_attempts.append(dict(work_package_id=work["id"], session_id=work["session_id"],
                                               event_cursor=row[0], candidate=row[1][:16000], truncated=len(row[1]) > 16000,
                                               status="unverified_candidate"))
            local = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"]))
            day = local.date().isoformat()
            if not stage and state["daily_stages"].get(day, 0) >= objective["stage_planning"]["max_new_stages_per_day"]:
                tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                state.update(state="waiting", reason="Daily new-stage limit reached", last_input=key,
                             next_review_at=tomorrow.astimezone(store.clock().tzinfo).isoformat())
                store.put(db, "agent_objective_planning", state)
                return False
            if objective["mode"] == "finite" and not objective["acceptance_criteria"]:
                state.update(state="awaiting_input", reason="Define observable completion criteria for this finite objective.",
                             question=dict(id="completion-" + oid, revision=state["revision"], field="completion_criteria",
                                           text="What observable result completes this objective?", options=[]))
                store.put(db, "agent_objective_planning", state)
                return False
            rid = "agent_run_plan_" + digest([aid, oid, key, state["revision"]])[:32]
            prior = db.execute("SELECT body FROM agent_runs WHERE id=?", (rid,)).fetchone()
            run = json.loads(prior[0]) if prior else dict(id=rid, agent_id=aid, objective_id=oid, kind="unified_plan",
                                                        state="planning", created_at=store.now())
            if prior and not run.get("decision"):
                state.update(state="waiting", reason="Planning response is unknown; revise with new information before retrying.", last_input=key)
                store.put(db, "agent_objective_planning", state)
                return False
            agent["planner_lease"] = rid
            store.put(db, "agents", agent)
            store.put(db, "agent_runs", run)
            payload = dict(planning_engine=2, objective=objective, stage=stage, evidence=records,
                           existing_goals=goals, history=history, answers=state["answers"], required_revision=state.get("required_revision"),
                           context=dict(source_routes=agent["role"]["source_routes"]), source_status=sources,
                           requested_goals=state.get("requested_goals", []),
                           revision_requested=state.get("revision_requested", False),
                           prior_attempts=prior_attempts[-3:],
                           required_verification=required_verification,
                           delivery_index=delivery_index([s for s in history if not stage or s["id"] != stage["id"]],
                               [g for g in goals if not stage or g.get("stage_id") != stage["id"]]))
        try:
            envs = environments(self.service, agent)
            payload["capabilities"] = capabilities(envs)
            payload["trigger"] = trigger
            payload["workspace_snapshot"] = probe(agent["role"]["working_directory"])
            from loom.autonomy.stage_planner import fit_payload

            payload = fit_payload(prepare_evidence(payload))
            with store.sessions.transaction() as db:
                run["input_snapshot_ref"] = store.artifact(db, aid, payload, "plan_input")
                store.put(db, "agent_runs", run)

            def checkpoint(value):
                with store.sessions.transaction() as db:
                    saved = store.get(db, "agent_runs", rid, aid)
                    saved["decision"] = value
                    store.put(db, "agent_runs", saved)

            if run.get("decision"):
                decision = run["decision"]
                compiled = compile_plan(self.service, agent, payload, decision, envs, rid, store.now())
            else:
                decision, compiled = self.planner.propose(agent, payload, envs, rid, checkpoint)
            # Re-resolve outside the transaction; permission/binding changes invalidate the draft.
            if capabilities(environments(self.service, agent)) != payload["capabilities"]:
                raise ServiceError("Environment changed while planning; re-evaluate the proposal", 409, code="SOURCE_CHANGED")
            with self.service.lock, store.sessions.transaction() as db:
                latest = store.get(db, "agents", aid)
                current = planning_state(store, db, latest, objective)
                current_stage = store.get(db, "agent_stages", stage["id"], aid) if stage else None
                actual_objective = objective_view(latest, next(o for o in latest["role"]["long_term_objectives"] if o["id"] == oid))
                valid = (latest.get("planning_engine") == 2 and latest["state"] == "enabled" and latest.get("planner_lease") == rid
                         and latest["generation"] == agent["generation"] and current["revision"] == state["revision"]
                         and input_key(latest, actual_objective, current, current_stage, planning_facts(store, db, aid),
                                       execution_facts(store, db, aid, oid), trigger) == key)
                saved = store.get(db, "agent_runs", rid, aid)
                if valid:
                    self.commit(db, latest, current, objective, decision, compiled, rid)
                    if payload["trigger"] and (compiled or decision["action"] == "wait"):
                        current["last_trigger"] = payload["trigger"]["key"]
                    published_stage = store.get(db, "agent_stages", current["stage_id"], aid) if current.get("stage_id") else None
                    if published_stage and published_stage["state"] in TERMINAL:
                        published_stage = None
                    current["last_input"] = (None if (decision["action"] == "review_stage" and decision["result"] == "completed"
                                                     or compiled and not compiled["work_packages"])
                        else input_key(latest, actual_objective, current, published_stage, planning_facts(store, db, aid),
                                       execution_facts(store, db, aid, oid), trigger))
                    store.put(db, "agent_objective_planning", current)
                    saved["state"] = "committed"
                else:
                    saved.update(state="superseded", reason="Planning inputs changed")
                if latest.get("planner_lease") == rid:
                    latest["planner_lease"] = None
                store.put(db, "agents", latest)
                store.put(db, "agent_runs", saved)
                store.event(db, aid, "plan.finished", dict(run_id=rid, state=saved["state"], objective_id=oid))
            return True
        except Exception as exc:
            with store.sessions.transaction() as db:
                latest = store.get(db, "agents", aid)
                current = planning_state(store, db, latest, objective)
                saved = store.get(db, "agent_runs", rid, aid)
                if latest.get("planner_lease") == rid:
                    latest["planner_lease"] = None
                    blocker = from_exception(exc, rid, key, None)
                    blocker.update(owner_ref=dict(kind="planning_attempt", id=rid),
                                   allowed_actions=[dict(type="replan", objective_id=oid, expected_revision=current["revision"] + 1)])
                    current.update(state="waiting", blocker=blocker, reason=str(exc), last_input=key,
                                   revision=current["revision"] + 1, next_review_at="")
                    if getattr(exc, "code", None) == "AGENT_BUDGET":
                        current["budget_key"] = budget_key
                        local = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"]))
                        tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                        current["next_review_at"] = tomorrow.astimezone(store.clock().tzinfo).isoformat()
                    elif getattr(exc, "retryable", False) and not isinstance(exc, ProtocolError):
                        attempts = current.get("transient_attempts", 0) + 1 if current.get("failure_input") == key else 1
                        current.update(transient_attempts=attempts, failure_input=key)
                        if attempts <= scheduling_policy(agent["role"])["max_recovery_attempts"]:
                            current["next_review_at"] = (store.clock() + timedelta(seconds=min(3600, 60 * 2 ** (attempts - 1)))).isoformat()
                    store.put(db, "agent_objective_planning", current)
                    store.put(db, "agents", latest)
                saved.update(state="failed", reason=str(exc))
                store.put(db, "agent_runs", saved)
                store.event(db, aid, "plan.rejected", dict(run_id=rid, objective_id=oid, reason=str(exc)))
            return False

    def commit(self, db, agent, state, objective, decision, compiled, rid):
        store, aid = self.store, agent["id"]
        action = decision["action"]
        state.update(revision=state["revision"] + 1, blocker=None, reason=decision["reason"])
        state.pop("budget_key", None)
        state.pop("transient_attempts", None)
        if compiled:
            old_stage = store.get(db, "agent_stages", state["stage_id"], aid) if state.get("stage_id") else None
            if old_stage and old_stage["state"] not in TERMINAL:
                self.retire(db, aid, old_stage)
            compiled["input_snapshot_ref"] = store.get(db, "agent_runs", rid, aid)["input_snapshot_ref"]
            stage = compiled["stage"]
            stage["role_signature"] = role_signature(agent["role"])
            store.put(db, "agent_plan_revisions", compiled)
            store.put(db, "agent_stages", stage)
            store.put(db, "agent_roadmaps", compiled["roadmap"])
            for goal in compiled["goals"]:
                store.put(db, "agent_goals", goal)
            for work in compiled["work_packages"]:
                store.put(db, "agent_work_packages", work)
                db.execute("INSERT OR IGNORE INTO agent_outbox VALUES(?,?,?,?,?)", (work["id"], aid, "dispatch", "pending", canonical(work)))
            state.update(state="active", stage_id=stage["id"], roadmap_id=stage["roadmap_id"], plan_id=compiled["id"],
                         next_review_at="", revision_requested=False)
            if not old_stage or old_stage["id"] != stage["id"]:
                state["no_progress"] = 0
                day = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()
                state["daily_stages"][day] = state["daily_stages"].get(day, 0) + 1
            state.pop("required_revision", None)
            wake(store, agent)
            store.event(db, aid, "plan.committed", dict(plan_id=compiled["id"], stage_id=stage["id"], objective_id=objective["id"]))
        elif action == "review_stage":
            stage = store.get(db, "agent_stages", state["stage_id"], aid)
            stage.update(state="completed" if decision["result"] == "completed" else "active", revision=stage["revision"] + 1)
            store.put(db, "agent_stages", stage)
            store.put(db, "agent_stage_reviews", dict(id="review_" + rid, agent_id=aid, stage_id=stage["id"], decision=decision, created_at=store.now()))
            state["state"] = "ready" if stage["state"] == "completed" else "waiting"
            if stage["state"] == "completed":
                state["no_progress"] = 0
                wake(store, agent)
            else:
                state["next_review_at"] = ""
                state["blocker"] = make(Code.REVIEW_GAP, stage["id"], decision["reason"])
                state["blocker"]["allowed_actions"] = [dict(type="replan", objective_id=objective["id"], expected_revision=state["revision"])]
        elif action == "complete_objective":
            state["state"] = "completed"
        elif action == "request_input":
            state.update(state="awaiting_input", question=dict(id="question_" + digest(rid)[:24], revision=state["revision"],
                         text=decision["question"], options=decision.get("options", [])))
        else:
            seconds = objective.get("schedule", {}).get("every_seconds", objective["stage_planning"]["review_seconds"])
            state.update(state="waiting", next_review_at=(store.clock() + timedelta(seconds=seconds)).isoformat())
            if state.get("revision_requested"):
                state["blocker"] = make(Code.EXECUTION_CONTRACT, state["id"], decision["reason"])

    def retire(self, db, aid, stage):
        for work in self.store.rows(db, "agent_work_packages", aid):
            if work.get("plan_id") != stage.get("plan_id") or work.get("superseded_contract"):
                continue
            if work.get("session_id"):
                session = self.store.sessions._load(db, work["session_id"])
                if (work["session_id"] in self.service.active or session["task"]["state"] in {"running", "queued", "pausing"}
                        or session.get("workspace_blocked") or (session.get("input_request") or {}).get("state") == "pending"):
                    raise ServiceError("Resolve in-flight execution or pending input before revising the plan", 409)
                if db.execute("SELECT 1 FROM operations WHERE session_id=? AND json_extract(body,'$.status')='started' "
                              "AND json_extract(body,'$.effect_kind')='side_effecting'", (work["session_id"],)).fetchone():
                    raise ServiceError("An execution result is unknown; reconcile it before revising", 409)
            if work["state"] != "succeeded":
                if work.get("session_id"):
                    from loom.autonomy.control import enqueue

                    enqueue(db, aid, "plan-retire:" + stage["plan_id"], work["session_id"], "stop_run", work["goal_id"])
                work.update(superseded_contract=True, state="cancelled")
                self.store.put(db, "agent_work_packages", work)
            db.execute("UPDATE agent_outbox SET state='superseded' WHERE id=? AND state='pending'", (work["id"],))
        for gid in stage["goal_ids"]:
            goal = self.store.get(db, "agent_goals", gid, aid)
            if goal["state"] != "completed":
                goal.update(state="cancelled", revision=goal["revision"] + 1)
                self.store.put(db, "agent_goals", goal)
