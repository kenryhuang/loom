"""One-time upgrade of pre-identity acceptance and untyped blockers.

Only this adapter may associate legacy descriptions. Historical revisions and
artifacts stay immutable; uncertain failures remain explicit manual blockers.
"""

import json

from loom.autonomy.acceptance import criteria, initialize, key, sync_objectives, verdict
from loom.autonomy.blockers import Code, make
from loom.service.contracts import canonical


def migrate(db):
    def rows(table):
        return {r[0]: json.loads(r[1]) for r in db.execute(f"SELECT id,body FROM {table}")}

    def save(table, record):
        db.execute(f"UPDATE {table} SET body=? WHERE id=?", (canonical(record), record["id"]))

    def bind(record, parents):
        if "acceptance_criteria" in record:
            return
        initialize(record)
        for i, description in enumerate(record.get("acceptance", [])):
            matches = {key(c): c for c in parents if c["text"] == description}
            if len(matches) == 1:
                record["acceptance_criteria"][i] = next(iter(matches.values()))

    agents, stages, goals, works = (rows(t) for t in ("agents", "agent_stages", "agent_goals", "agent_work_packages"))
    for agent in agents.values():
        sync_objectives(agent)
        save("agents", agent)
    for stage in stages.values():
        parent = agents.get(stage["agent_id"], {}).get("objective_contracts", {}).get(stage["objective_id"], {}).get("criteria", [])
        bind(stage, parent)
        save("agent_stages", stage)
    for goal in goals.values():
        bind(goal, stages.get(goal.get("stage_id"), {}).get("acceptance_criteria", []))
        save("agent_goals", goal)
    for work in works.values():
        if work.get("blocked_reason") == "recovery_exhausted":
            work.setdefault("recovery", {})["exhausted"] = True
            work["blocker"] = make(Code.RECOVERY_EXHAUSTED, work["id"], "Automatic recovery attempts exhausted", phase="execution")
        bind(work, goals.get(work["goal_id"], {}).get("acceptance_criteria", []))
        if "acceptance_result" not in work and work.get("terminal_run_id") and work.get("outcome"):
            sid, rid, cursor = work.get("session_id"), work["terminal_run_id"], work["outcome"]["event_cursor"]
            gate = db.execute(
                "SELECT body FROM events WHERE session_id=? AND seq<=? AND json_extract(body,'$.run_id')=? "
                "AND json_extract(body,'$.type') IN ('acceptance.gate.passed','acceptance.gate.blocked') ORDER BY seq DESC LIMIT 1",
                (sid, cursor, rid),
            ).fetchone()
            from loom.autonomy.acceptance import contract_key

            work["acceptance_result"] = verdict(work, rid, sid, cursor, json.loads(gate[0]) if gate else None,
                                                pinned=contract_key(work["acceptance_criteria"]))
            work["acceptance_result"]["origin"] = "legacy_gate_migration"
        save("agent_work_packages", work)

    planning = rows("agent_objective_planning")
    for state in planning.values():
        if state["state"] == "completed":
            from loom.autonomy.acceptance import objective_view
            from loom.autonomy.store import digest

            agent = agents[state["agent_id"]]
            objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == state["objective_id"]), None)
            if objective:
                state["objective_contract"] = digest(objective_view(agent, objective))

        if state.get("required_revision") and "acceptance_criteria" not in state["required_revision"]:
            requested = state["required_revision"]
            requested["acceptance_criteria"] = criteria(state["id"] + ":revision", requested["acceptance"], state["revision"])
        if "blocker" not in state:
            state["blocker"] = None
            if state["state"] in {"blocked", "deferred", "awaiting_input"}:
                code = Code.INPUT_REQUIRED if state["state"] == "awaiting_input" else Code.LEGACY_UNCLASSIFIED
                state["blocker"] = make(code, state["id"], state.get("reason", "Legacy planning stop"),
                                        details=dict(migration_version=5, previous_state=state["state"]))
        state.setdefault("retry_requested", False)
        save("agent_objective_planning", state)

    # A recorded v1 response is not silently reinterpreted as a v2 decision.
    for run in rows("agent_runs").values():
        if run.get("kind") in {"stage_plan", "stage_review"} and run["state"] in {"planning", "recoverable"}:
            run["legacy_decision"] = run.pop("decision", None)
            run["state"] = "failed"
            code = Code.LEGACY_UNCLASSIFIED if run["legacy_decision"] else Code.UNKNOWN_RESPONSE
            run["failure"] = make(code, run["id"], "Legacy planning response requires explicit recovery")
            save("agent_runs", run)
            for state in planning.values():
                if (state["agent_id"] == run["agent_id"] and state["objective_id"] == run.get("objective_id")
                        and not state["manual_pause"] and state["state"] not in {"cancelled", "completed", "awaiting_input"}):
                    state.update(state="blocked", blocker=make(code, state["id"], run["failure"]["message"]), retry_requested=False)
                    save("agent_objective_planning", state)
            agent = agents[run["agent_id"]]
            if agent.get("planner_lease") == run["id"]:
                agent["planner_lease"] = None
                save("agents", agent)

    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone():
        return
    for session in rows("sessions").values():
        binding, run = session.get("autonomy") or {}, session.get("run") or {}
        work = works.get(binding.get("work_package_id"))
        if work and binding.get("agent_id") == work["agent_id"]:
            from loom.autonomy.acceptance import contract_key

            binding.setdefault("acceptance_contract", contract_key(work["acceptance_criteria"]))
        # Legacy fixed host messages are translated once, never on the live recovery path.
        stop_codes = {"Token budget exceeded": "TOKEN_BUDGET_EXCEEDED", "LLM token budget exceeded": "TOKEN_BUDGET_EXCEEDED",
                      "Active time budget exceeded": "ACTIVE_TIME_BUDGET_EXCEEDED"}
        if "stop_code" not in run and run.get("reason") in stop_codes:
            run["stop_code"] = stop_codes[run["reason"]]
        if work:
            from loom.autonomy.legacy_budget import migrate_budget_failure

            agent = agents.get(work["agent_id"])
            if agent:
                migrate_budget_failure(db, agent, work, session, goals.get(work["goal_id"], {}))
                saved = db.execute("SELECT body FROM agent_budget_blocks WHERE id=?", ("execution:" + work["id"],)).fetchone()
                if saved:
                    denial = json.loads(saved[0])
                    failure = run.get("failure") or {}
                    if (denial.get("binding", {}).get("run_id") == run.get("id")
                            and failure.get("code") == "LLM_FAILED" and denial.get("reason") == failure.get("message")):
                        failure["legacy_code"] = failure["code"]
                        failure["code"] = "AGENT_BUDGET"
        db.execute("UPDATE sessions SET body=? WHERE id=?", (canonical(session), session["session_id"]))
