"""Read-only execution projection: facts, blockers and concrete recovery steps."""

import json
from datetime import datetime

from loom.autonomy.blockers import instructions, manual_required
from loom.autonomy.contracts import learning_policy, scheduling_policy


def analysis_diagnostics(db, cycle):
    """Project diagnostics in SQLite; never deserialize the evidence checkpoint."""
    row = db.execute("SELECT json_object('state',json_extract(body,'$.state'), 'usage',json_extract(body,'$.usage'), "
                     "'settings',json_extract(body,'$.settings'), 'error_details',json_extract(body,'$.error_details')) "
                     "FROM semantic_jobs WHERE id=?", (cycle.get('evaluation_id'),)).fetchone()
    result = json.loads(row[0]) if row else {}
    for field in ('usage', 'settings'):
        result[field] = result.get(field) or {}
    result.update(evaluation_id=cycle.get('evaluation_id'), units=[], truncated=False)
    errors = result.get('error_details') or []
    result['error_details'] = errors[:20]
    result['truncated'] = len(errors) > 20
    for path in ('$.checkpoint.units', '$.checkpoint.memory.units'):
        rows = db.execute("SELECT json_object('id',coalesce(json_extract(u.value,'$.id'),u.key), "
                          "'disposition',json_extract(u.value,'$.disposition'), 'issues', "
                          "CASE WHEN json_array_length(json_extract(u.value,'$.issues'))>0 THEN json_extract(u.value,'$.issues') "
                          "ELSE coalesce(json_extract(u.value,'$.errors'),json('[]')) END) "
                          "FROM semantic_jobs j,json_each(j.body,?) u WHERE j.id=? AND "
                          "(json_array_length(json_extract(u.value,'$.issues'))>0 OR "
                          "json_array_length(json_extract(u.value,'$.errors'))>0 OR "
                          "json_extract(u.value,'$.disposition') IN ('abstained','quarantined','exhausted')) LIMIT 101",
                          (path, cycle.get('evaluation_id'))).fetchall()
        result['units'].extend(json.loads(r[0]) for r in rows[:100])
        result['truncated'] |= len(rows) > 100
    return result


def report(store, service, db, agent, *, analysis_details=True):
    aid = agent["id"]
    active = service.active.copy()
    policy = scheduling_policy(agent["role"])
    works = agent["work_packages"]
    goals = {g["id"]: g for g in agent["goals"]}
    sources = {s["route_id"]: s for s in agent.get("source_status", [])}
    routes = {r["id"]: r for r in agent["role"]["source_routes"]}
    blocks, runtime, sessions = [], [], {}

    def block(code, scope, reason, details, steps, *, automatic=False, action=None):
        blocks.append(
            dict(id=f"{code}:{scope}", code=code, scope_id=scope, reason=reason, details=details, next_steps=steps, automatic=automatic, action=action)
        )

    if not agent.get("service_enabled", True):
        block("SERVICE_DISABLED", aid, "Autonomy service not enabled", [], ["Start the service on the original data directory with --enable-agents."])
    scheduler = agent.get("scheduler", {})
    if scheduler.get("manual_required"):
        block("SCHEDULER_INTERVENTION", scheduler.get("work_package_id") or aid, scheduler.get("reason", "Scheduling needs manual intervention"),
              [f"Last check: {scheduler.get('finished_at')}"], scheduler.get("next_steps", []))
    if agent["state"] != "enabled":
        block("AGENT_PAUSED", aid, f"Agent state: {agent['state']}", [], ["Resume or enable the Agent from its header; a manual pause is never overridden by the timer."])
    for error in ("error", "service_error", "scheduling_error", "background_error"):
        if agent.get(error):
            block(
                "RUNTIME_" + error.upper(),
                aid,
                str(agent[error]),
                [],
                ["Check the original errors of the tasks and Sessions below, fix them, then run a check; do not bypass failures by recreating tasks."],
            )
    for goal in goals.values():
        if goal["state"] in {"completed", "cancelled", "superseded"}:
            continue
        if goal["state"] in {"blocked", "paused"}:
            block(
                "GOAL_" + goal["state"].upper(),
                goal["id"],
                f"Goal {goal['state']}: {goal['objective']}",
                [str(goal.get("blocked_reason") or goal.get("reason") or "see the goal's execution results and stage records")],
                ["Check the failure or pause reason on the Goals tab; retry with changed input, or manually resume the paused goal."],
            )
        if goal.get("not_before", "") > store.now():
            block(
                "GOAL_BACKOFF",
                goal["id"],
                "Goal is waiting for its retry window",
                [f"Earliest run: {goal['not_before']}"],
                ["Automatically checked once the retry time arrives and preconditions are met."],
                automatic=True,
            )
        if goal.get("stage_id"):
            stage = next((s for s in agent.get("stages", []) if s["id"] == goal["stage_id"]), {})
            if stage.get("state") in {"paused", "blocked", "cancelled", "superseded"}:
                block(
                    "STAGE_NOT_RUNNABLE",
                    goal["id"],
                    f"Owning stage is not runnable: {stage.get('title', goal['stage_id'])}",
                    [f"Stage state: {stage.get('state')}; reason: {stage.get('reason') or 'manual or stage control change'}"],
                    ["Check the stage state on the goal roadmap; resume or replan after fixing the blocker - work packages from the old stage are never force-run."],
                )
        for rid in goal.get("required_source_ids", []):
            source = sources.get(rid)
            fallback = [sources.get(f) for f in routes.get(rid, {}).get("fallback_route_ids", [])]
            if (source and not source["revalidate_before_use"]) or any(s and not s["revalidate_before_use"] for s in fallback):
                continue
            route = routes.get(rid)
            details = [f"Goal: {goal['objective']}", f"Source: {rid}", f"Bound paths: {', '.join((route or {}).get('paths', [])) or 'no local paths'}"]
            if source:
                details += [
                    f"State: {source['state']}; last checked: {source.get('checked_at')}; valid until: {source.get('expires_at')}",
                    f"Invalid items: {', '.join(source.get('invalid_document_ids', [])) or 'none'}",
                ]
                details += [str(i.get("source", "")) + ": " + str(i.get("error", "")) for i in source.get("issues", [])]
            else:
                details.append("This source has no verification record yet." if route else "This source is no longer in the current Role's bindings.")
            block(
                "SOURCE_NOT_CURRENT",
                goal["id"] + ":" + rid,
                "A required source is unavailable or expired",
                details,
                ["Verify paths, knowledge bases, and permissions under Knowledge; fix the entries that no longer exist.", "Refresh bound sources; if content or the Role changed, review and replan the stage."],
                automatic=bool(route and route.get("refresh_seconds")),
                action="refresh_sources",
            )
        pending = [goals.get(g, {"id": g, "state": "missing"}) for g in goal.get("depends_on", []) if goals.get(g, {}).get("state") != "completed"]
        if pending:
            block(
                "GOAL_DEPENDENCY",
                goal["id"],
                "Waiting for prerequisite goal acceptance",
                [f"{g.get('objective', g['id'])}: {g['state']}" for g in pending],
                ["Resolve the prerequisite goals' blockers first; scheduling continues automatically once they pass acceptance."],
                automatic=True,
            )
        if goal.get("source_changed"):
            block(
                "SOURCE_CHANGED",
                goal["id"],
                "A source changed after planning",
                [f"Source event: {goal['source_changed']}"],
                ["Review the source change and revise or replan the stage; existing execution results are kept."],
            )
        for lid in goal.get("learning_dependency", []):
            cycle = next((c for c in agent["learning"] if c["id"] == lid), {})
            if cycle.get("state") != "completed" or cycle.get("coverage") != "full":
                block(
                    "LEARNING_DEPENDENCY",
                    goal["id"] + ":" + lid,
                    "Waiting for a required analysis",
                    [f"Analysis {lid}: {cycle.get('state', 'missing')} / {cycle.get('coverage', 'none')}"],
                    ["Check the background analysis budget, retry count, and rejections; execution continues automatically once the analysis is complete."],
                    automatic=cycle.get("state") in {"running", "pending"},
                )
    for state in agent.get("objective_planning", []):
        if state["state"] in {"blocked", "awaiting_input", "deferred"}:
            block(
                (state.get("blocker") or {}).get("code") or "STAGE_" + state["state"].upper(),
                state["id"],
                state.get("reason") or state["state"],
                [f"Long-term objective: {state['objective_id']}", f"Stage: {state.get('stage_id') or 'not generated yet'}"],
                instructions(state["blocker"]) if state.get("blocker") else ["Check the legacy state and retry manually."],
                automatic=bool(state.get("blocker")) and not manual_required(state["blocker"]),
            )
    for work in works:
        if work["state"] == "pending":
            predecessors = [
                w
                for w in works
                if w["goal_id"] == work["goal_id"]
                and not w.get("superseded_contract")
                and w.get("order", 0) < work.get("order", 0)
                and w["state"] != "succeeded"
            ]
            if predecessors:
                block(
                    "WORK_DEPENDENCY",
                    work["id"],
                    "Waiting for earlier work packages of the same goal",
                    [f"{w['objective']}: {w['state']} ({w['id']})" for w in predecessors],
                    ["Finish the earlier work packages first; saving a slice checkpoint is not acceptance."],
                    automatic=True,
                )
        if not work.get("session_id"):
            if work.get("blocked_reason"):
                block(
                    "DISPATCH_BLOCKED",
                    work["id"],
                    work["blocked_reason"],
                    [f"Work package Role revision: {work.get('role_revision')}; current revision: {agent['role_revision']}"],
                    ["Check this work package's preconditions; after configuration or contract changes, revise or replan on the Goals tab - old contracts are never force-dispatched."],
                )
            continue
        try:
            sid = work['session_id']
            if sid not in sessions:
                row = db.execute("SELECT json_object('session_id',id, 'task',json_object('state',json_extract(body,'$.task.state')), "
                    "'run',json_object('id',json_extract(body,'$.run.id'),'state',json_extract(body,'$.run.state'), "
                    "'steps',json_extract(body,'$.run.steps'),'active_seconds',json_extract(body,'$.run.active_seconds'), "
                    "'reason',json_extract(body,'$.run.reason'),'failure',json_extract(body,'$.run.failure'), "
                    "'checkpoint',json_type(body,'$.run.checkpoint') IS NOT NULL AND json_type(body,'$.run.checkpoint')<>'null'), "
                    "'context',json_type(body,'$.context') IS NOT NULL AND json_type(body,'$.context')<>'null', "
                    "'autonomy',json_extract(body,'$.autonomy'),'control',json_extract(body,'$.control'), "
                    "'workspace_blocked',json_extract(body,'$.workspace_blocked'),'input_request',json_extract(body,'$.input_request')) "
                    "FROM sessions WHERE id=? AND json_extract(body,'$.deleted_at') IS NULL", (sid,)).fetchone()
                if row is None:
                    raise ValueError('Session not found')
                sessions[sid] = json.loads(row[0])
            state = sessions[sid]
        except Exception as exc:
            block("SESSION_MISSING", work["id"], str(exc), [work["session_id"]], ["Restore the original Session and data directory; never treat unknown execution results as not run."])
            continue
        run = state.get("run") or {}
        if run.get("id") not in work.get("run_ids", []) and work["state"] in {"succeeded", "failed", "cancelled"}:
            if work["state"] == "failed":
                block(
                    "WORK_FAILED",
                    work["id"],
                    (work.get("outcome") or {}).get("reason") or "Historical work package execution failed",
                    [f"Original run: {work.get('terminal_run_id')}; the Session has taken over later work."],
                    ["Open the original execution record to find this run's failure; retry the goal with changed input."],
                )
            continue  # A reused Session's current run must not be shown as this work's progress.
        item = dict(
            work_package_id=work["id"],
            session_id=state["session_id"],
            task_state=state["task"]["state"],
            run_id=run.get("id"),
            run_state=run.get("state"),
            steps=run.get("steps") or 0,
            active_seconds=run.get("active_seconds") or 0,
            checkpoint_available=bool(run.get("checkpoint") or state.get("context")),
            worker_active=state["session_id"] in active,
            reason=run.get("reason"),
        )
        info = work.get("slice", {})
        if info.get("phase") in {"paused", "resuming"}:
            from loom.autonomy.stages import role_signature

            pinned = store.get(db, "agent_roles", f"{aid}:{state['autonomy']['role_revision']}", aid)["role"]
            if role_signature(pinned) != role_signature(agent["role"]):
                block(
                    "RESUME_ROLE_CHANGED",
                    work["id"],
                    "The Role's execution contract changed while paused",
                    [f"Session Role revision: {state['autonomy']['role_revision']}; current revision: {agent['role_revision']}"],
                    ["Review the Role's responsibility, tool, or source changes and replan the affected tasks; the timer cannot apply a new contract to an old checkpoint."],
                )
            if (state.get("control") or {}).get("command_id") != info.get("pause_command_id"):
                block(
                    "RESUME_CONTROL_CHANGED",
                    work["id"],
                    "A manual or other recovery control arrived after the pause; auto-resume has yielded",
                    [f"Current control: {(state.get('control') or {}).get('command_id')}"],
                    ["Check the original Session and resume it manually; human control takes precedence."],
                )
        if info.get("phase") == "held":
            block("SLICE_HELD", work["id"], info.get("reason") or "Slice resume needs attention", [], ["Open the original Session to check the pause control and checkpoint; continue after confirming recovery."])
        if info.get("phase") == "running":
            item["slice_elapsed_seconds"] = max(0, (store.clock() - datetime.fromisoformat(info["started_at"])).total_seconds())
        runtime.append(item)
        if work["state"] in {"succeeded", "cancelled"}:
            continue
        if state["task"]["state"] == "queued":
            block(
                "SESSION_ADMISSION",
                work["id"],
                "Waiting for a Session execution slot or workspace lock",
                [f"Active service workers: {len(active)}/{service.capacity}", f"Active Sessions: {', '.join(active) or 'none; waiting for the next service dispatch'}"],
                ["The service dispatches automatically when a slot is free and resources do not conflict; do not recreate the same task."],
                automatic=True,
            )
        link = next((s for s in agent["sessions"] if s["session_id"] == work["session_id"]), {})
        if link.get("taken_over"):
            block("HUMAN_TAKEOVER", work["id"], "Session was taken over by a human", [state["session_id"]], ["After finishing manual work, return the Session under Session management."])
        if state.get("workspace_blocked") or (state.get("input_request") or {}).get("state") == "pending":
            request = state.get("input_request") or {}
            block(
                "SESSION_INPUT",
                work["id"],
                "Execution needs input or side-effect recovery confirmation",
                [str(request.get("question") or request.get("prompt") or request), str(state.get("workspace_blocked") or "")],
                ["Open the original Session, verify the tool results, and answer pending questions; the timer never guesses whether side effects completed."],
            )
        elif info.get("phase") in {"pausing", "paused", "resuming"}:
            pass  # Expected checkpoint handoff is progress, not an execution failure.
        elif state["task"]["state"] in {"failed", "paused", "recovering"}:
            block(
                "SESSION_" + state["task"]["state"].upper(),
                work["id"],
                run.get("reason") or work.get("blocked_reason") or state["task"]["state"],
                [f"Session：{state['session_id']}", f"run：{run.get('id')}", str(run.get("failure") or "")],
                ["Open the Session to check the original failure or pause reason.", "Manual pauses need manual resume; failures need a stated change before retry - slice scheduling never bypasses budgets, input, or recovery requirements."],
            )
    for cycle in agent["learning"]:
        if cycle["state"] in {"failed", "deferred", "incomplete"}:
            evidence = []
            job = analysis_diagnostics(db, cycle) if analysis_details and cycle.get('evaluation_id') else {}
            if job.get('state'):
                evidence.append(f"Deep Eval: {job['state']}; Calls {job.get('usage', {}).get('calls', 0)}/{job.get('settings', {}).get('max_calls', 'unknown')}")
                evidence += [str(e) for e in job.get("error_details", [])]
                for unit in job['units']:
                    issues = unit.get("issues") or unit.get("errors") or []
                    if issues or unit.get("disposition") in {"abstained", "quarantined", "exhausted"}:
                        evidence.append(f"{unit.get('id', 'analysis unit')} / {unit.get('disposition')}: {issues or 'insufficient evidence or an unknown conclusion; not a complete pass'}")
            block(
                "ANALYSIS_" + cycle["state"].upper(),
                cycle["id"],
                cycle.get("blocked_reason") or cycle["state"],
                [
                    f"Evaluation: {cycle.get('evaluation_id')}",
                    f"Attempts this round: {cycle.get('attempts', 0) - cycle.get('round_start_attempts', 0)}/{learning_policy(agent['role'])['max_attempts']}; "
                    f"total {cycle.get('attempts', 0)}",
                    f"Next retry allowed: {cycle.get('retry_after') or 'changed input required'}",
                    *evidence,
                ],
                ["See the specific Deep Eval failures and accepted records.", "Budget can auto-expand within existing caps; after hitting the attempt cap, no progress, or the hard budget cap, fix the cause or adjust the configuration before retrying."],
                automatic=bool(cycle.get("auto_resume")),
            )
    return dict(
        policy=policy,
        next_tick_at=agent.get("next_schedule_at"),
        scheduler=scheduler,
        blockers=blocks,
        runtime=runtime,
        learning_policy=learning_policy(agent["role"]),
        history=[
            json.loads(r[0])
            for r in db.execute(
                "SELECT body FROM agent_events WHERE agent_id=? AND (json_extract(body,'$.type') LIKE 'slice.%' "
                "OR json_extract(body,'$.type') LIKE 'learning.%' "
                "OR json_extract(body,'$.type') LIKE 'schedule.%' "
                "OR json_extract(body,'$.type') LIKE 'planning.%' OR json_extract(body,'$.type') LIKE 'stage.planning.%') ORDER BY seq DESC LIMIT 40",
                (aid,),
            )
        ],
    )
