"""Durable objective-to-stage orchestration over existing Goals and workers."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from loom.autonomy.acceptance import inherit, initialize, objective_view, recorded_verdict, ref_key
from loom.autonomy.acceptance import key as criterion_key
from loom.autonomy.blockers import RECONCILED, Code, from_exception, holds, make
from loom.autonomy.contracts import goal_contract, stage_enabled
from loom.autonomy.execution_budget import execution_block, objective_limit
from loom.autonomy.stage_overlap import delivery_index, validate_overlap
from loom.autonomy.store import digest
from loom.service.contracts import ServiceError

TERMINAL = {"completed", "cancelled", "superseded"}


def state_id(aid, oid):
    return "objective_plan_" + digest([aid, oid])[:32]


def planning_state(store, db, agent, objective):
    identifier = state_id(agent["id"], objective["id"])
    row = db.execute("SELECT body FROM agent_objective_planning WHERE id=?", (identifier,)).fetchone()
    if row:
        return json.loads(row[0])
    return dict(
        id=identifier,
        agent_id=agent["id"],
        objective_id=objective["id"],
        revision=1,
        state="new",
        roadmap_id=None,
        stage_id=None,
        discovery_id=None,
        no_progress=0,
        daily_stages={},
        last_input=None,
        next_review_at="",
        answers=[],
        manual_pause=False,
        blocker=None,
        retry_requested=False,
    )


def evidence(store, db, aid, goals):
    """Bounded excerpts of real tool outputs; every reference retains its run/cursor."""
    ids = {g["id"] for g in goals}
    rows = []
    for work in store.rows(db, "agent_work_packages", aid):
        if work["goal_id"] not in ids or work.get("superseded_contract") or not work.get("outcome"):
            continue
        sid, rid = work.get("session_id"), work.get("terminal_run_id")
        if not sid or not rid:
            continue
        cursor = work["outcome"]["event_cursor"]
        events = db.execute(
            "SELECT body FROM events WHERE session_id=? AND seq<=? AND json_extract(body,'$.run_id')=? "
            "AND json_extract(body,'$.type') IN ('tool.completed','acceptance.gate.passed','acceptance.gate.blocked') "
            "ORDER BY seq DESC LIMIT 12",
            (sid, cursor, rid),
        ).fetchall()
        outputs = []
        for event_row in reversed(events):
            event = json.loads(event_row[0])
            if event["type"] != "tool.completed":
                continue
            payload = event["payload"]
            ref = payload.get("artifact", {})
            if ref.get("kind") == "event_detail":
                try:
                    payload = json.loads(store.sessions.artifacts.read(ref["sha256"]))
                except (OSError, ValueError, ServiceError):
                    continue
            outputs.append(dict(seq=event["seq"], excerpt=json.dumps(payload, ensure_ascii=False)[:1800]))
        if outputs:
            rows.append(
                dict(
                    id="delivery:" + work["id"],
                    kind="delivery",
                    work_package_id=work["id"],
                    goal_id=work["goal_id"],
                    run_id=rid,
                    session_id=sid,
                    event_cursor=cursor,
                    acceptance=work["acceptance"],
                    passed=recorded_verdict(db, work),
                    verified_criteria=work.get("acceptance_result", {}).get("criteria", []),
                    acceptance_criteria=work.get("acceptance_criteria", []),
                    outcome=work["outcome"],
                    created_at=work.get("created_at"),
                    recorded_outputs=outputs[-4:],
                )
            )
    return rows[-12:]


def signature(agent, objective, goals, observations, sources, habits):
    return digest(
        dict(
            role_revision=agent["role_revision"],
            objective=objective,
            guidance=agent["guidance_version"],
            goals=[(g["id"], g["revision"], g["state"], g.get("source_changed"),
                    [(c["id"], c["revision"]) for c in g.get("acceptance_criteria", [])]) for g in goals],
            observations=observations,
            sources=[(s["id"], s.get("revision"), s.get("digest"), s.get("state")) for s in sources],
            habits=habits,
        )
    )


def role_signature(role):
    """Budget changes and enable switches do not change an existing delivery contract."""
    from copy import deepcopy

    value = deepcopy(role)
    for key in ("budget", "learning_policy", "scheduling_policy"):
        value.pop(key, None)
    for objective in value["long_term_objectives"]:
        for key in ("objective_tokens", "mode", "max_new_stages_per_day"):
            objective.get("stage_planning", {}).pop(key, None)
    return digest(value)


def runnable(store, db, agent, goal):
    if not goal.get("stage_id") and not goal.get("stage_discovery"):
        return True
    objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == goal["long_term_objective_id"]), None)
    if not objective or not stage_enabled(objective):
        return False
    state = planning_state(store, db, agent, objective)
    if state["manual_pause"] or state["state"] in {"awaiting_input", "blocked", "cancelled", "completed"}:
        return False
    if goal.get("stage_id"):
        stage = store.get(db, "agent_stages", goal["stage_id"], agent["id"])
        return stage["state"] == "active" and goal["id"] in stage["goal_ids"]
    return True


class StageCoordinator:
    def __init__(self, service, store, planner):
        self.service, self.store, self.planner = service, store, planner

    def save(self, db, record, **changes):
        if changes.get("state") not in {None, "blocked", "deferred", "awaiting_input"}:
            changes.setdefault("blocker", None)
        if all(record.get(key) == value for key, value in changes.items()):
            return
        record.update(**changes, revision=record["revision"] + 1)
        self.store.put(db, "agent_objective_planning", record)

    def block(self, db, state, code, message, fingerprint=None, **changes):
        blocker = make(code, state["id"], message, dependency_key=fingerprint, details=changes.pop("details", None))
        self.save(db, state, state="blocked", reason=message, blocker=blocker, last_input=fingerprint, **changes)

    def reconcile_state(self, db, agent, state, stage, own, fingerprint):
        if state["manual_pause"] or state["state"] in {"awaiting_input", "cancelled", "completed"}:
            return False
        if stage and stage["state"] == "paused":
            return False
        if stage and stage["state"] not in TERMINAL:
            missing = set(stage["goal_ids"]) - {g["id"] for g in own}
            if missing:
                self.block(db, state, Code.GOAL_MISSING, "Stage references missing Goals", fingerprint, details=dict(goal_ids=sorted(missing)))
                return False
            changed = [g for g in own if g.get("source_changed") and g["state"] not in TERMINAL]
            if changed:
                self.block(db, state, Code.SOURCE_CHANGED, "Required source changed; review or replace the affected contract", fingerprint,
                           details=dict(goals=[dict(goal_id=g["id"], event_id=g["source_changed"]) for g in changed]))
                return False
            if stage.get("role_signature") != role_signature(agent["role"]):
                self.block(db, state, Code.ROLE_CHANGED, "Role changed; review the active stage contract", fingerprint)
                return False
            if any(g["state"] in {"cancelled", "superseded"} for g in own):
                self.block(db, state, Code.GOAL_TERMINAL, "Stage Goal failed or was cancelled; changed input is required", fingerprint)
                return False
        blocker = state.get("blocker")
        if blocker and blocker["code"] in RECONCILED:
            self.save(db, state, state="active" if stage and stage["state"] not in TERMINAL else "ready", last_input=None)
            if stage and stage["state"] == "blocked":
                stage.update(state="active", revision=stage["revision"] + 1)
                self.store.put(db, "agent_stages", stage)
        return True

    def reconcile(self, aid):
        """No model calls: reconcile structural facts before queue recovery/dispatch."""
        with self.store.sessions.transaction() as db:
            agent = self.store.get(db, "agents", aid)
            if agent["state"] != "enabled" or agent.get("planner_lease"):
                return
            for state in self.store.rows(db, "agent_objective_planning", aid):
                objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == state["objective_id"]), None)
                if not objective or not stage_enabled(objective):
                    continue
                goals = [g for g in self.store.rows(db, "agent_goals", aid) if g["long_term_objective_id"] == objective["id"]]
                stage = self.store.get(db, "agent_stages", state["stage_id"], aid) if state.get("stage_id") else None
                own = [g for g in goals if stage and g["id"] in stage["goal_ids"]]
                fingerprint = digest([agent["role_revision"], [(g["id"], g["revision"], g["state"], g.get("source_changed")) for g in own]])
                self.reconcile_state(db, agent, state, stage, own, fingerprint)

    def discovery(self, db, agent, objective, state, reason, task=None):
        gap = digest(
            [
                role_signature(agent["role"]),
                objective["id"],
                task,
                [(s["id"], s.get("version")) for s in self.store.rows(db, "agent_sources", agent["id"])],
                [o["id"] for o in self.store.rows(db, "agent_observations", agent["id"]) if o["objective_id"] == objective["id"]],
                int(self.store.clock().timestamp()) // objective["stage_planning"]["review_seconds"],
            ]
        )
        if state.get("discovery_id"):
            previous = self.store.get(db, "agent_goals", state["discovery_id"], agent["id"])
            if previous["state"] != "completed" or gap == state.get("discovery_gap"):
                raise ServiceError("Baseline investigation already attempted; provide new evidence or revise its Goal", 409)
        gid = "goal_discovery_" + digest([agent["id"], objective["id"], state["revision"]])[:32]
        criteria = (task or {}).get("acceptance", ["Read actual implementation and design files; cite recorded tool outputs and identify unknowns"])
        goal = dict(
            **goal_contract(
                dict(
                    objective=(task or {}).get(
                        "objective",
                        "Inspect the current implementation and existing delivery evidence for: "
                        + objective["desired_outcome"]
                        + ". Produce a bounded baseline and concrete gaps; do not modify files.",
                    ),
                    acceptance=criteria,
                    long_term_objective_id=objective["id"],
                ),
                agent["role"],
            ),
            id=gid,
            agent_id=agent["id"],
            revision=1,
            state="queued",
            created_at=self.store.now(),
            producer="stage_discovery",
            stage_discovery=True,
            read_only=True,
        )
        self.store.put(db, "agent_goals", goal)
        self.save(db, state, discovery_id=gid, discovery_gap=gap, state="discovering", reason=reason, no_progress=state["no_progress"] + (1 if task else 0))
        self.wake(db, agent)
        self.store.event(db, agent["id"], "stage.discovery.created", dict(objective_id=objective["id"], goal_id=gid))

    def wake(self, db, agent):
        agent.update(generation=agent["generation"] + 1, next_wake_at=self.store.now())
        self.store.put(db, "agents", agent)

    def advance(self, aid):
        from loom.autonomy.configuration import activate

        with self.store.sessions.transaction() as db:
            if not activate(self.store, db, aid):
                return
        snapshot = self.store.snapshot(aid)
        served = {s["objective_id"]: s.get("last_planned_at", "") for s in snapshot["objective_planning"]}
        for objective in sorted(snapshot["role"]["long_term_objectives"], key=lambda o: served.get(o["id"], "")):
            if stage_enabled(objective) and self._advance(aid, objective["id"]):
                break  # At most one model decision per Agent tick; no polling loop of calls.

    def _advance(self, aid, oid):
        store = self.store
        with store.sessions.transaction() as db:
            agent = store.get(db, "agents", aid)
            objective = objective_view(agent, next(o for o in agent["role"]["long_term_objectives"] if o["id"] == oid))
            if agent["state"] != "enabled" or agent.get("planner_lease"):
                return False
            state = planning_state(store, db, agent, objective)
            if state["manual_pause"] or state["state"] in {"awaiting_input", "cancelled"}:
                return False
            goals = [g for g in store.rows(db, "agent_goals", aid) if g["long_term_objective_id"] == oid]
            observations = [o for o in store.rows(db, "agent_observations", aid) if o["objective_id"] == oid]
            sources = store.rows(db, "agent_sources", aid)
            habits = [p for p in store.rows(db, "agent_profile", aid) if p.get("habit", {}).get("objective_id") == oid]
            fingerprint = signature(agent, objective, goals, observations, sources, habits)
            budget_key = digest(
                [
                    agent["role"]["budget"],
                    objective_limit(agent, objective),
                    store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat(),
                ]
            )
            if state["state"] == "completed":
                if state.get("objective_contract") == digest(objective):
                    return False
                self.save(db, state, state="ready", last_input=None)
            stage = store.get(db, "agent_stages", state["stage_id"], aid) if state.get("stage_id") else None
            own = [g for g in goals if stage and g["id"] in stage["goal_ids"]]
            if not self.reconcile_state(db, agent, state, stage, own, fingerprint):
                return False
            blocker = state.get("blocker")
            if holds(blocker, fingerprint, budget_key, retry_requested=state.get("retry_requested", False)):
                return False
            if blocker:
                # Structural predicates were checked above. Every recovery uses typed policy,
                # dependency versions or an explicit retry, never the display reason.
                self.save(db, state, state="active" if stage else "ready", last_input=None, retry_requested=False)
                if stage and stage["state"] == "blocked" and blocker["code"] in RECONCILED:
                    stage.update(state="active", revision=stage["revision"] + 1)
                    store.put(db, "agent_stages", stage)
            if state["last_input"] == fingerprint and state.get("next_review_at", "") > store.now():
                return False
            if state.get("retry_after", "") > store.now():
                return False
            if stage and stage["state"] == "blocked" and state["last_input"] == fingerprint:
                return False
            if stage and stage["state"] not in TERMINAL:
                failed = any(g["state"] == "blocked" for g in own)
                if failed:
                    for work in store.rows(db, "agent_work_packages", aid):
                        if work["goal_id"] in stage["goal_ids"] and work["state"] == "failed" and work.get("session_id"):
                            saved = db.execute("SELECT body FROM sessions WHERE id=?", (work["session_id"],)).fetchone()
                            if saved and execution_block(store, db, agent, work, json.loads(saved[0])):
                                return False  # Budget stops belong to queue recovery, not model acceptance review.
                    live = [
                        w
                        for w in store.rows(db, "agent_work_packages", aid)
                        if w["goal_id"] in stage["goal_ids"] and w.get("session_id") and w["state"] in {"running", "dispatched", "blocked"}
                    ]
                    if live:
                        return False  # Resolve paused/input/recovery work through its existing protocol.
                elif not own or any(g["state"] != "completed" for g in own):
                    return False
                if stage["state"] != "reviewing":
                    stage.update(state="reviewing", revision=stage["revision"] + 1)
                    store.put(db, "agent_stages", stage)
            else:
                stage = None
                if any(g["state"] not in TERMINAL and not g.get("stage_id") and g.get("producer") != "stage_discovery" for g in goals):
                    self.save(db, state, state="waiting", reason="Finish or resolve existing objective Goals before creating overlapping work")
                    return False
                backlog = [c for c in store.rows(db, "agent_learning_cycles", aid) if c["state"] not in {"completed", "cancelled", "failed", "incomplete"}]
                if len(backlog) >= 20:
                    self.save(
                        db,
                        state,
                        state="waiting",
                        reason="Learning backlog reached 20; resolve pending learning before new stages",
                        last_input=fingerprint,
                        next_review_at="",
                    )
                    return False
            if state["no_progress"] >= objective["stage_planning"]["max_replans_without_progress"] and (
                not stage or any(g["state"] == "blocked" for g in goals if g["id"] in stage["goal_ids"])
            ):
                self.block(db, state, Code.NO_PROGRESS, "No-progress limit reached; provide new input", fingerprint)
                return False
            if not stage and objective["mode"] != "finite":
                day = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()
                if state["daily_stages"].get(day, 0) >= objective["stage_planning"]["max_new_stages_per_day"]:
                    # Avoid nested ledger transaction; derive the local next midnight directly.
                    local = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"]))
                    next_day = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                    self.save(
                        db,
                        state,
                        state="waiting",
                        reason="Daily new-stage limit reached",
                        last_input=fingerprint,
                        next_review_at=next_day.astimezone(store.clock().tzinfo).isoformat(),
                    )
                    return False
            if state.get("discovery_id"):
                discovery = next(g for g in goals if g["id"] == state["discovery_id"])
                if discovery["state"] not in TERMINAL | {"blocked"}:
                    return False
            records = evidence(store, db, aid, goals)
            fresh = any(
                e.get("created_at")
                and (store.clock() - datetime.fromisoformat(e["created_at"])).total_seconds() <= objective["stage_planning"]["review_seconds"]
                for e in records
            )
            if objective["mode"] == "finite" and not objective["stage_planning"]["completion_criteria"]:
                self.save(
                    db,
                    state,
                    state="awaiting_input",
                    reason="Finite objective needs final completion criteria",
                    blocker=make(Code.INPUT_REQUIRED, state["id"], "Finite objective needs final completion criteria"),
                    question=dict(
                        id="question_" + digest([state["id"], state["revision"]])[:24],
                        revision=1,
                        text="What observable result completes this objective?",
                        options=[],
                        field="completion_criteria",
                    ),
                )
                return False
            if not records or not fresh and not stage:
                if not state.get("discovery_id") or discovery["state"] == "completed" and records and not fresh:
                    self.discovery(db, agent, objective, state, "No recorded implementation baseline")
                else:
                    discovery = next(g for g in goals if g["id"] == state["discovery_id"])
                    if discovery["state"] in TERMINAL | {"blocked"}:
                        self.block(
                            db, state, Code.BASELINE_MISSING, "Baseline investigation has no recorded tool evidence; resolve its Goal", fingerprint
                        )
                return False
            review_goals = stage["goal_ids"] if stage else []
            cycles = [c for c in store.rows(db, "agent_learning_cycles", aid) if c["goal_id"] in review_goals]
            goal_fields = ("id", "objective", "acceptance", "state", "stage_id", "source_changed", "acceptance_criteria", "contract_revision")
            payload = dict(
                objective=objective,
                stage=stage,
                evidence=[*records, *[dict(id="observation:" + o["id"], kind="observation", record=o) for o in observations[-10:]]],
                existing_goals=[{k: g.get(k) for k in goal_fields} for g in goals][-30:],
                source_changes=[e for e in store.rows(db, "agent_external_events", aid) if e["id"] in {g.get("source_changed") for g in own}],
                observations=observations[-10:],
                habits=habits[-5:],
                source_status=sources,
                history=[s for s in store.rows(db, "agent_stages", aid) if s["objective_id"] == oid][-10:],
                roadmap=store.get(db, "agent_roadmaps", state["roadmap_id"], aid) if state.get("roadmap_id") else None,
                answers=state["answers"][-5:],
                required_revision=state.get("required_revision"),
                repair=state.get("repair"),
                learning=[{k: c.get(k) for k in ("id", "goal_id", "state", "coverage", "blocked_reason")} for c in cycles],
                delivery_index=delivery_index(
                    [s for s in store.rows(db, 'agent_stages', aid) if s['objective_id'] == oid],
                    store.rows(db, 'agent_goals', aid)),
            )
            from loom.autonomy.stage_planner import acceptance_support

            payload["acceptance_support"] = acceptance_support(payload)
            kind = "stage_review" if stage else "stage_plan"
            key = digest([aid, oid, fingerprint, stage and stage["revision"], state["revision"]])
            rid = "agent_run_stage_" + key[:32]
            old = db.execute("SELECT body FROM agent_runs WHERE id=?", (rid,)).fetchone()
            run = (
                json.loads(old[0])
                if old
                else dict(
                    id=rid,
                    agent_id=aid,
                    kind=kind,
                    objective_id=oid,
                    state="planning",
                    generation=agent["generation"],
                    role_revision=agent["role_revision"],
                    created_at=store.now(),
                )
            )
            if old and not run.get("decision"):
                self.save(
                    db,
                    state,
                    state="blocked",
                    blocker=make(Code.UNKNOWN_RESPONSE, state["id"], "Previous response was not recorded; usage may be unknown", dependency_key=fingerprint),
                    reason="Previous planning response was not recorded; usage may be unknown. Replan with an explanation.",
                    last_input=fingerprint,
                )
                return False
            agent["planner_lease"] = rid
            state["last_planned_at"] = store.now()
            store.put(db, "agent_objective_planning", state)
            store.put(db, "agents", agent)
            store.put(db, "agent_runs", run)
        try:
            from loom.autonomy.context import build_snapshot

            payload["context"] = build_snapshot(store, aid, purpose=kind)
            with store.sessions.transaction() as db:
                run["input_snapshot_ref"] = store.artifact(db, aid, payload, kind="stage_planning_input")
                store.put(db, "agent_runs", run)

            def checkpoint(decision):
                with store.sessions.transaction() as db:
                    saved = store.get(db, "agent_runs", rid, aid)
                    saved["decision"] = decision
                    store.put(db, "agent_runs", saved)

            decision = run.get("decision") or self.planner.propose(agent, payload, checkpoint)
            with store.sessions.transaction() as db:
                latest = store.get(db, "agents", aid)
                current = planning_state(store, db, latest, objective)
                current_goals = [g for g in store.rows(db, "agent_goals", aid) if g["long_term_objective_id"] == oid]
                current_observations = [o for o in store.rows(db, "agent_observations", aid) if o["objective_id"] == oid]
                current_habits = [p for p in store.rows(db, "agent_profile", aid) if p.get("habit", {}).get("objective_id") == oid]
                valid = (
                    latest["state"] == "enabled"
                    and latest.get("planner_lease") == rid
                    and latest["generation"] == agent["generation"]
                    and current["revision"] == state["revision"]
                    and signature(latest, objective, current_goals, current_observations, store.rows(db, "agent_sources", aid), current_habits) == fingerprint
                )
                run = store.get(db, "agent_runs", rid, aid)
                if valid:
                    self.commit(db, latest, objective, current, payload, decision, rid)
                    current.update(last_input=fingerprint, objective_contract=digest(objective))
                    if current.get("blocker") and current["blocker"].get("dependency_key") is None:
                        current["blocker"]["dependency_key"] = fingerprint
                    store.put(db, "agent_objective_planning", current)
                    run["state"] = "committed"
                else:
                    run.update(state="superseded", reason="Planning inputs changed")
                if latest.get("planner_lease") == rid:
                    latest["planner_lease"] = None
                store.put(db, "agents", latest)
                store.put(db, "agent_runs", run)
                store.event(db, aid, "stage.planning.finished", dict(agent_run_id=rid, state=run["state"], objective_id=oid))
        except Exception as exc:
            with store.sessions.transaction() as db:
                latest = store.get(db, "agents", aid)
                current = planning_state(store, db, latest, objective)
                if latest.get("planner_lease") == rid:
                    latest["planner_lease"] = None
                    store.put(db, "agents", latest)
                    blocker = from_exception(exc, current["id"], fingerprint, budget_key)
                    deferred = blocker["code"] == Code.PLANNING_BUDGET
                    self.save(
                        db,
                        current,
                        state="deferred" if deferred else "blocked",
                        reason=str(exc),
                        blocker=blocker,
                        last_input=fingerprint,
                        budget_key=budget_key,
                        no_progress=current["no_progress"] + (0 if deferred else 1),
                    )
                run = store.get(db, "agent_runs", rid, aid)
                run.update(state="failed", reason=str(exc), failure=from_exception(exc, current["id"], fingerprint, budget_key))
                store.put(db, "agent_runs", run)
                store.event(db, aid, "stage.planning.failed", dict(agent_run_id=rid, reason=str(exc)))
        return True

    def commit(self, db, agent, objective, state, payload, decision, run_id):
        from loom.autonomy.stage_planner import validate

        validate(decision, payload)
        action, policy = decision["action"], objective["stage_planning"]
        store, aid = self.store, agent["id"]
        if payload.get("stage") and action not in {"review_stage", "wait", "request_input"}:
            raise ServiceError("Review the active stage before proposing a successor")
        changes = dict(reason=decision["reason"], next_review_at="")
        if action in {"review_stage", "complete_objective"}:
            stage = payload.get("stage")
            completed = action == "complete_objective" or decision["result"] == "completed"
            if completed and stage and any(g["state"] != "completed" for g in payload["existing_goals"] if g["id"] in stage["goal_ids"]):
                raise ServiceError("Stage still has unfinished or failed Goals")
            review = dict(
                id="review_" + digest(run_id)[:32],
                agent_id=aid,
                objective_id=objective["id"],
                stage_id=stage and stage["id"],
                stage_revision=stage and stage["revision"],
                decision=decision,
                learning=payload["learning"],
                created_at=store.now(),
            )
            store.put(db, "agent_stage_reviews", review)
            if stage and decision.get("result") == "repair":
                self.repair(db, agent, state, stage, decision, review["id"], policy)
                return
            if stage:
                stage.update(state="completed" if completed else "blocked", revision=stage["revision"] + 1, review_id=review["id"], reason=decision["reason"])
                store.put(db, "agent_stages", stage)
            changes.update(
                state="completed" if action == "complete_objective" else "ready" if completed else "blocked",
                blocker=None if completed else make(Code.REVIEW_GAP, state["id"], decision["reason"], details=dict(review_id=review["id"])),
                no_progress=0 if completed else state["no_progress"] + 1,
            )
            self.wake(db, agent)
        elif action == "propose_stage":
            stage = dict(decision["stage"])
            requested = state.get("required_revision")
            if requested:
                stage["expected_outcome"] = requested["expected_outcome"]
            existing = [s for s in store.rows(db, "agent_stages", aid) if s["objective_id"] == objective["id"]]
            if (
                state["daily_stages"].get(store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat(), 0)
                >= policy["max_new_stages_per_day"]
            ):
                local = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"]))
                next_day = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                self.save(
                    db, state, state="waiting", reason="Daily new-stage limit reached", next_review_at=next_day.astimezone(store.clock().tzinfo).isoformat()
                )
                return
            original = store.rows(db, "agent_goals", aid)
            validate_overlap(stage, delivery_index(existing, original))
            sid = "stage_" + digest(run_id)[:32]
            stage["id"] = sid
            initialize(stage)
            covered = {ref_key(r) for r in stage.get("covers", [])}
            inherit(stage, [c for c in objective["acceptance_criteria"] if criterion_key(c) in covered])
            if requested:
                inherit(stage, requested["acceptance_criteria"])
            roadmap = dict(
                id="roadmap_" + digest(run_id)[:32],
                agent_id=aid,
                objective_id=objective["id"],
                revision=len(existing) + 1,
                role_revision=agent["role_revision"],
                baseline=decision["baseline"],
                source_refs=decision["source_refs"],
                future=decision.get("future", []),
                stage_id=sid,
                supersedes=state.get("roadmap_id"),
                input_run_id=run_id,
                reason=decision["reason"],
                created_at=store.now(),
            )
            goal_ids, mapping = [], {}
            for index, item in enumerate(stage.pop("goals")):
                # Integration Goal inherits every stage criterion, not a model's weaker subset.
                criteria = item["acceptance"]
                gid = item.get("reuse_goal_id") or "goal_stage_" + digest([sid, item["key"]])[:32]
                if item.get("reuse_goal_id"):
                    goal = store.get(db, "agent_goals", gid, aid)
                    if (
                        goal.get("stage_id")
                        or goal["long_term_objective_id"] != objective["id"]
                        or goal["state"] != "completed"
                        or goal["contract_revision"] != item.get("reuse_contract_revision")
                        or index == len(decision["stage"]["goals"]) - 1
                        and not {criterion_key(c) for c in stage["acceptance_criteria"]} <= {criterion_key(c) for c in goal["acceptance_criteria"]}
                    ):
                        raise ServiceError("Only compatible completed unassigned Goals can be reused")
                else:
                    goal = dict(
                        **goal_contract(
                            dict(
                                objective=item["objective"],
                                acceptance=criteria,
                                long_term_objective_id=objective["id"],
                                depends_on=list(dict.fromkeys([*goal_ids, *[mapping[k] for k in item["depends_on"]]])),
                                stop_conditions=stage["stop_conditions"],
                                required_source_ids=stage.get("required_source_ids", []),
                            ),
                            agent["role"],
                        ),
                        id=gid,
                        agent_id=aid,
                        revision=1,
                        state="queued",
                        created_at=store.now(),
                        producer="stage_planner",
                        stage_id=sid,
                        roadmap_id=roadmap["id"],
                    )
                    initialize(goal)
                    if index == len(decision["stage"]["goals"]) - 1:
                        inherit(goal, stage["acceptance_criteria"])
                    store.put(db, "agent_goals", goal)
                mapping[item["key"]] = gid
                goal_ids.append(gid)
            stage.update(
                id=sid,
                agent_id=aid,
                objective_id=objective["id"],
                revision=1,
                state="active",
                goal_ids=goal_ids,
                roadmap_id=roadmap["id"],
                role_revision=agent["role_revision"],
                role_signature=role_signature(agent["role"]),
                predecessor_id=state.get("stage_id"),
                created_at=store.now(),
            )
            store.put(db, "agent_roadmaps", roadmap)
            store.put(db, "agent_stages", stage)
            day = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()
            state["daily_stages"][day] = state["daily_stages"].get(day, 0) + 1
            changes.update(state="active", stage_id=sid, roadmap_id=roadmap["id"])
            state.pop("required_revision", None)
            self.wake(db, agent)
            store.event(db, aid, "stage.activated", dict(stage_id=sid, goal_ids=goal_ids))
        elif action == "request_input":
            changes.update(
                state="awaiting_input",
                blocker=make(Code.INPUT_REQUIRED, state["id"], decision["reason"]),
                question=dict(id="question_" + digest(run_id)[:24], revision=1, text=decision["question"], options=decision.get("options", [])),
            )
        elif action == "investigate":
            self.discovery(db, agent, objective, state, decision["reason"], decision)
            return
        else:
            intervals = [policy["review_seconds"], objective.get("schedule", {}).get("every_seconds", policy["review_seconds"])]
            intervals.extend(h["habit"]["every_seconds"] for h in payload["habits"] if h.get("state") == "active")
            changes.update(state="waiting", next_review_at=(store.clock() + timedelta(seconds=min(intervals))).isoformat())
        self.save(db, state, **changes)

    def repair(self, db, agent, state, stage, decision, review_id, policy):
        """A changed method creates bounded successor Goals; old failures remain facts."""
        store, aid = self.store, agent["id"]
        old = [store.get(db, "agent_goals", gid, aid) for gid in stage["goal_ids"]]
        failed = [g for g in old if g["state"] == "blocked"]
        # If all Goals passed but the stage review found a genuine integration gap,
        # add a new integration contract instead of rewriting their successes.
        targets = failed or [old[-1]]
        fingerprint = digest([stage["id"], decision["change"], decision["remaining_gap"]])
        if fingerprint in state.get("repair_fingerprints", []) or state["no_progress"] >= policy["max_replans_without_progress"]:
            raise ServiceError("Repeated method or no-progress limit reached")
        state.setdefault("repair_fingerprints", []).append(fingerprint)
        replacements = {}
        retry_at = (store.clock() + timedelta(seconds=60 * 2 ** state["no_progress"])).isoformat()
        for target in targets:
            new = dict(target)
            new.update(
                id="goal_stage_retry_" + digest([fingerprint, target["id"]])[:32],
                revision=1,
                state="queued",
                created_at=store.now(),
                predecessor_id=target["id"],
                retry_root=target.get("retry_root", target["id"]),
                attempt=target.get("attempt", 0) + 1,
                change=decision["change"],
                change_kind="method",
                not_before=retry_at,
                objective=target["objective"] + "\nChanged method: " + decision["change"] + "\nRemaining gap: " + decision["remaining_gap"],
            )
            inherit(new, stage["acceptance_criteria"])
            new.pop("source_changed", None)
            store.put(db, "agent_goals", new)
            replacements[target["id"]] = new["id"]
        for goal in old:
            if goal["state"] == "queued" and any(dep in replacements for dep in goal["depends_on"]):
                goal["depends_on"] = [replacements.get(dep, dep) for dep in goal["depends_on"]]
                goal["revision"] += 1
                store.put(db, "agent_goals", goal)
        stage.update(goal_ids=[replacements.get(gid, gid) for gid in stage["goal_ids"]], state="active", revision=stage["revision"] + 1, review_id=review_id)
        store.put(db, "agent_stages", stage)
        self.save(
            db,
            state,
            state="active",
            reason=decision["reason"],
            no_progress=state["no_progress"] + 1,
            retry_after=retry_at,
            repair=dict(change=decision["change"], remaining_gap=decision["remaining_gap"], review_id=review_id),
        )
        self.wake(db, agent)
        store.event(db, aid, "stage.repair.scheduled", dict(stage_id=stage["id"], replacements=replacements, retry_after=retry_at))
