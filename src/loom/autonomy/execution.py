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
        block("SERVICE_DISABLED", aid, "自主服务未启用", [], ["使用 --enable-agents 启动原数据目录的服务。"])
    scheduler = agent.get("scheduler", {})
    if scheduler.get("manual_required"):
        block("SCHEDULER_INTERVENTION", scheduler.get("work_package_id") or aid, scheduler.get("reason", "调度需要人工介入"),
              [f"最近检查：{scheduler.get('finished_at')}"], scheduler.get("next_steps", []))
    if agent["state"] != "enabled":
        block("AGENT_PAUSED", aid, f"Agent 状态为 {agent['state']}", [], ["在 Agent 顶部恢复或启用；人工暂停不会被定时器覆盖。"])
    for error in ("error", "service_error", "scheduling_error", "background_error"):
        if agent.get(error):
            block(
                "RUNTIME_" + error.upper(),
                aid,
                str(agent[error]),
                [],
                ["核对下方任务和 Session 的原始错误，修复后点击立即检查；不要通过反复重建任务绕过失败。"],
            )
    for goal in goals.values():
        if goal["state"] in {"completed", "cancelled", "superseded"}:
            continue
        if goal["state"] in {"blocked", "paused"}:
            block(
                "GOAL_" + goal["state"].upper(),
                goal["id"],
                f"目标{goal['state']}：{goal['objective']}",
                [str(goal.get("blocked_reason") or goal.get("reason") or "见该目标的执行结果和阶段记录")],
                ["在目标页核对失败或暂停原因；补充变化输入后重试，或手动恢复暂停的目标。"],
            )
        if goal.get("not_before", "") > store.now():
            block(
                "GOAL_BACKOFF",
                goal["id"],
                "目标正在等待重试窗口",
                [f"最早执行：{goal['not_before']}"],
                ["到达重试时间且前置条件满足后自动检查。"],
                automatic=True,
            )
        if goal.get("stage_id"):
            stage = next((s for s in agent.get("stages", []) if s["id"] == goal["stage_id"]), {})
            if stage.get("state") in {"paused", "blocked", "cancelled", "superseded"}:
                block(
                    "STAGE_NOT_RUNNABLE",
                    goal["id"],
                    f"所属阶段不可执行：{stage.get('title', goal['stage_id'])}",
                    [f"阶段状态：{stage.get('state')}；原因：{stage.get('reason') or '人工或阶段控制变更'}"],
                    ["在目标路线图检查阶段状态；修复卡点后恢复或重新规划，旧阶段的工作包不会强行执行。"],
                )
        for rid in goal.get("required_source_ids", []):
            source = sources.get(rid)
            fallback = [sources.get(f) for f in routes.get(rid, {}).get("fallback_route_ids", [])]
            if (source and not source["revalidate_before_use"]) or any(s and not s["revalidate_before_use"] for s in fallback):
                continue
            route = routes.get(rid)
            details = [f"目标：{goal['objective']}", f"来源：{rid}", f"绑定路径：{', '.join((route or {}).get('paths', [])) or '无本地路径'}"]
            if source:
                details += [
                    f"状态：{source['state']}；上次核验：{source.get('checked_at')}；有效至：{source.get('expires_at')}",
                    f"失效项：{', '.join(source.get('invalid_document_ids', [])) or '无'}",
                ]
                details += [str(i.get("source", "")) + ": " + str(i.get("error", "")) for i in source.get("issues", [])]
            else:
                details.append("该来源尚无核验记录。" if route else "该来源已不在当前 Role 的绑定范围内。")
            block(
                "SOURCE_NOT_CURRENT",
                goal["id"] + ":" + rid,
                "必需来源不可用或已过期",
                details,
                ["在知识来源核对路径、知识库和权限，修正不存在的项目。", "刷新已绑定来源；若内容或 Role 已变更，复核并重新规划该阶段。"],
                automatic=bool(route and route.get("refresh_seconds")),
                action="refresh_sources",
            )
        pending = [goals.get(g, {"id": g, "state": "missing"}) for g in goal.get("depends_on", []) if goals.get(g, {}).get("state") != "completed"]
        if pending:
            block(
                "GOAL_DEPENDENCY",
                goal["id"],
                "等待前置目标验收",
                [f"{g.get('objective', g['id'])}：{g['state']}" for g in pending],
                ["先处理前置目标的卡点；前置目标验收通过后自动调度。"],
                automatic=True,
            )
        if goal.get("source_changed"):
            block(
                "SOURCE_CHANGED",
                goal["id"],
                "来源在规划后发生变化",
                [f"来源事件：{goal['source_changed']}"],
                ["查看来源变更，修订或重新规划阶段；已有执行结果保留。"],
            )
        for lid in goal.get("learning_dependency", []):
            cycle = next((c for c in agent["learning"] if c["id"] == lid), {})
            if cycle.get("state") != "completed" or cycle.get("coverage") != "full":
                block(
                    "LEARNING_DEPENDENCY",
                    goal["id"] + ":" + lid,
                    "等待指定分析产出",
                    [f"分析 {lid}：{cycle.get('state', 'missing')} / {cycle.get('coverage', 'none')}"],
                    ["检查后台分析的预算、重试次数与拒绝记录；完整分析后自动继续。"],
                    automatic=cycle.get("state") in {"running", "pending"},
                )
    for state in agent.get("objective_planning", []):
        if state["state"] in {"blocked", "awaiting_input", "deferred"}:
            block(
                (state.get("blocker") or {}).get("code") or "STAGE_" + state["state"].upper(),
                state["id"],
                state.get("reason") or state["state"],
                [f"长期目标：{state['objective_id']}", f"阶段：{state.get('stage_id') or '尚未生成'}"],
                instructions(state["blocker"]) if state.get("blocker") else ["检查旧版状态并手动重试。"],
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
                    "等待同一目标的前序工作包验收",
                    [f"{w['objective']}：{w['state']} ({w['id']})" for w in predecessors],
                    ["先完成前序工作包；保存时间片 checkpoint 不等于完成验收。"],
                    automatic=True,
                )
        if not work.get("session_id"):
            if work.get("blocked_reason"):
                block(
                    "DISPATCH_BLOCKED",
                    work["id"],
                    work["blocked_reason"],
                    [f"工作包 Role 版本：{work.get('role_revision')}；当前版本：{agent['role_revision']}"],
                    ["核对该工作包的前置条件；配置或合同变化后在目标页修订或重新规划，不能强行派发旧合同。"],
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
            block("SESSION_MISSING", work["id"], str(exc), [work["session_id"]], ["恢复原 Session 与数据目录，禁止将未知执行结果当作未执行。"])
            continue
        run = state.get("run") or {}
        if run.get("id") not in work.get("run_ids", []) and work["state"] in {"succeeded", "failed", "cancelled"}:
            if work["state"] == "failed":
                block(
                    "WORK_FAILED",
                    work["id"],
                    (work.get("outcome") or {}).get("reason") or "历史工作包执行失败",
                    [f"原 run：{work.get('terminal_run_id')}；Session 已承接后续工作。"],
                    ["打开原执行记录定位此 run 的失败原因；提供变化输入后重试对应目标。"],
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
                    "暂停期间 Role 的执行合同发生变化",
                    [f"Session Role 版本：{state['autonomy']['role_revision']}；当前版本：{agent['role_revision']}"],
                    ["核对 Role 的职责、工具或来源变化；重新规划对应任务，定时器不能把新合同套用到旧 checkpoint。"],
                )
            if (state.get("control") or {}).get("command_id") != info.get("pause_command_id"):
                block(
                    "RESUME_CONTROL_CHANGED",
                    work["id"],
                    "暂停后收到人工或其他恢复控制，自动续跑已让出",
                    [f"当前控制：{(state.get('control') or {}).get('command_id')}"],
                    ["在原 Session 检查并手动恢复；人工控制优先。"],
                )
        if info.get("phase") == "held":
            block("SLICE_HELD", work["id"], info.get("reason") or "时间片续跑需要处理", [], ["打开原 Session 核对暂停控制和 checkpoint；完成恢复确认后继续。"])
        if info.get("phase") == "running":
            item["slice_elapsed_seconds"] = max(0, (store.clock() - datetime.fromisoformat(info["started_at"])).total_seconds())
        runtime.append(item)
        if work["state"] in {"succeeded", "cancelled"}:
            continue
        if state["task"]["state"] == "queued":
            block(
                "SESSION_ADMISSION",
                work["id"],
                "等待 Session 执行槽位或工作目录锁",
                [f"服务活动 worker：{len(active)}/{service.capacity}", f"当前活动 Session：{', '.join(active) or '无；等待下一次服务调度'}"],
                ["服务在槽位空闲且资源无冲突时自动派发；不要重建同一任务。"],
                automatic=True,
            )
        link = next((s for s in agent["sessions"] if s["session_id"] == work["session_id"]), {})
        if link.get("taken_over"):
            block("HUMAN_TAKEOVER", work["id"], "Session 已被人工接管", [state["session_id"]], ["完成手工处理后，在 Session 管理中归还 Agent。"])
        if state.get("workspace_blocked") or (state.get("input_request") or {}).get("state") == "pending":
            request = state.get("input_request") or {}
            block(
                "SESSION_INPUT",
                work["id"],
                "执行需要输入或副作用恢复确认",
                [str(request.get("question") or request.get("prompt") or request), str(state.get("workspace_blocked") or "")],
                ["打开原 Session，核实已有工具结果，回答待答问题；定时器不会自动猜测副作用是否完成。"],
            )
        elif info.get("phase") in {"pausing", "paused", "resuming"}:
            pass  # Expected checkpoint handoff is progress, not an execution failure.
        elif state["task"]["state"] in {"failed", "paused", "recovering"}:
            block(
                "SESSION_" + state["task"]["state"].upper(),
                work["id"],
                run.get("reason") or work.get("blocked_reason") or state["task"]["state"],
                [f"Session：{state['session_id']}", f"run：{run.get('id')}", str(run.get("failure") or "")],
                ["打开 Session 检查失败或暂停的原始原因。", "人工暂停需手动恢复；失败需提供变化原因后重试，时间片调度不会绕过预算、输入和恢复要求。"],
            )
    for cycle in agent["learning"]:
        if cycle["state"] in {"failed", "deferred", "incomplete"}:
            evidence = []
            job = analysis_diagnostics(db, cycle) if analysis_details and cycle.get('evaluation_id') else {}
            if job.get('state'):
                evidence.append(f"Deep Eval：{job['state']}；Calls {job.get('usage', {}).get('calls', 0)}/{job.get('settings', {}).get('max_calls', '未知')}")
                evidence += [str(e) for e in job.get("error_details", [])]
                for unit in job['units']:
                    issues = unit.get("issues") or unit.get("errors") or []
                    if issues or unit.get("disposition") in {"abstained", "quarantined", "exhausted"}:
                        evidence.append(f"{unit.get('id', '分析单元')} / {unit.get('disposition')}：{issues or '证据不足或结论为 unknown，不能视为完整完成'}")
            block(
                "ANALYSIS_" + cycle["state"].upper(),
                cycle["id"],
                cycle.get("blocked_reason") or cycle["state"],
                [
                    f"评估：{cycle.get('evaluation_id')}",
                    f"本轮分析次数：{cycle.get('attempts', 0) - cycle.get('round_start_attempts', 0)}/{learning_policy(agent['role'])['max_attempts']}；"
                    f"累计 {cycle.get('attempts', 0)}",
                    f"下次允许重试：{cycle.get('retry_after') or '需要变化输入'}",
                    *evidence,
                ],
                ["查看 Deep Eval 的具体失败项和已接受记录。", "预算可在现有上限内自动扩容；达到次数、无进展或硬预算上限后，修复原因或调整配置再重试。"],
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
