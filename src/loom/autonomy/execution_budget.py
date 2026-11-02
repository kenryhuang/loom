"""Bounded work/objective allocations; cumulative usage and daily/Goal caps remain authoritative."""

import json
from zoneinfo import ZoneInfo

from loom.autonomy.contracts import scheduling_policy
from loom.service.contracts import ServiceError


def objective_limit(agent, objective):
    base = objective["stage_planning"]["objective_tokens"]
    ceiling = max(base, scheduling_policy(agent["role"])["max_objective_tokens"])
    granted = agent.get("objective_budgets", {}).get(objective["id"], {}).get("tokens", base)
    return max(base, min(granted, ceiling))


def objective_used(db, aid, oid):
    return db.execute(
        "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM agent_budget_calls WHERE agent_id=? AND "
        "(id IN (SELECT call_id FROM agent_budget_objectives WHERE agent_id=? AND objective_id=?) OR "
        "goal_id IN (SELECT id FROM agent_goals WHERE agent_id=? AND json_extract(body,'$.long_term_objective_id')=?))",
        (aid, aid, oid, aid, oid),
    ).fetchone()[0]


def expand_objective(store, db, agent, goal, block):
    objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == goal["long_term_objective_id"]), {})
    if not objective.get("stage_planning"):
        return
    used = objective_used(db, agent["id"], objective["id"])
    before = objective_limit(agent, objective)
    needed = used + block["requested"]
    if needed <= before:
        return  # A replay or a manually raised budget needs no additional grant.
    policy = scheduling_policy(agent["role"])
    if not policy["auto_expand_objective_budget"]:
        raise ServiceError(f"长期目标预算自动扩容已关闭：已用及预留 {used}，本次需要 {block['requested']}，当前额度 {before}", 409)
    ceiling = max(objective["stage_planning"]["objective_tokens"], policy["max_objective_tokens"])
    after = min(ceiling, max(before * 2, needed))
    if after < needed:
        raise ServiceError(f"长期目标自动扩容上限不足：已用及预留 {used}，本次需要 {block['requested']}，上限 {ceiling} token", 409)
    allocation = agent.setdefault("objective_budgets", {}).setdefault(objective["id"], dict(initial_tokens=before, grants=[]))
    grant = dict(before=before, after=after, used=used, requested=block["requested"], block_id=block["id"],
                 run_id=block["binding"]["run_id"], at=store.now())
    allocation.update(tokens=after, ceiling=ceiling, grants=[*allocation["grants"], grant])
    store.put(db, "agents", agent)
    store.event(db, agent["id"], "schedule.objective_budget.expanded", dict(objective_id=objective["id"], **grant))


def execution_block(store, db, agent, work, state):
    """Use a host denial bound to this run; display messages are irrelevant."""
    owner = state.get("autonomy") or {}
    if any(owner.get(k) != v for k, v in dict(agent_id=agent["id"], goal_id=work["goal_id"], work_package_id=work["id"]).items()):
        return None
    run = state.get("run") or {}
    binding = dict(agent_id=agent["id"], goal_id=work["goal_id"], work_package_id=work["id"], run_id=run.get("id"))
    if (run.get("failure") or {}).get("code") != "AGENT_BUDGET":
        return None
    row = db.execute("SELECT body FROM agent_budget_blocks WHERE id=?", ("execution:" + work["id"],)).fetchone()
    if row:
        saved = json.loads(row[0])
        if all(saved["binding"].get(k) == v for k, v in binding.items()):
            return saved
    return None


def restore_budget_stage(store, db, agent, goal):
    """Undo an unfinished review of a budget-stopped Goal, never human controls."""
    from loom.autonomy.stages import planning_state, role_signature

    if not goal.get("stage_id") or agent.get("planner_lease"):
        return False
    objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == goal["long_term_objective_id"]), {})
    if objective.get("stage_planning", {}).get("mode") != "autonomous":
        return False
    planning = planning_state(store, db, agent, objective)
    stage = store.get(db, "agent_stages", goal["stage_id"], agent["id"])
    if (planning["manual_pause"] or planning["state"] not in {"active", "deferred", "waiting"}
            or planning.get("stage_id") != stage["id"] or stage["state"] != "reviewing" or stage.get("review_id")
            or stage.get("role_signature") != role_signature(agent["role"]) or goal["id"] not in stage["goal_ids"]):
        return False
    siblings = [g for g in store.rows(db, "agent_goals", agent["id"]) if g["id"] in stage["goal_ids"] and g["id"] != goal["id"]]
    if any(g["state"] in {"blocked", "paused", "cancelled", "superseded"}
           or (g.get("source_changed") and g["state"] != "completed") for g in siblings):
        return False
    stage.update(state="active", revision=stage["revision"] + 1)
    planning.update(state="active", reason="Budget-stopped execution returned to the scheduler", revision=planning["revision"] + 1,
                    last_input=None, retry_after="", next_review_at="", blocker=None)
    store.put(db, "agent_stages", stage)
    store.put(db, "agent_objective_planning", planning)
    store.event(db, agent["id"], "schedule.budget_stage.restored", dict(stage_id=stage["id"], goal_id=goal["id"]))
    return True


def allocate(store, db, agent, work, *, requested=None):
    policy = scheduling_policy(agent["role"])
    base = agent["role"]["budget"]["work_tokens"]
    old = work.get("execution_budget", {})
    before = old.get("tokens", base)
    ceiling = max(base, policy["max_work_tokens"])
    if requested is None:
        estimate = work.get("estimated_tokens", base)
        target = min(ceiling, max(base, (estimate * 5 + 3) // 4 if work.get("token_estimate_basis") == "model" else estimate))
    else:
        if not policy["auto_expand_work_budget"]:
            raise ServiceError("工作包自动扩容已关闭；请调整工作预算后手动调度", 409)
        target = min(ceiling, max(before * 2, requested))
        if target <= before or target < requested:
            raise ServiceError(f"工作包扩容上限不足：当前 {before}，需要 {requested}，上限 {ceiling} token；请调整上限后手动调度", 409)
    work["execution_budget"] = dict(
        tokens=max(before, target),
        initial_tokens=old.get("initial_tokens", target),
        ceiling=ceiling,
        grants=[*old.get("grants", []), *([dict(before=before, after=target, at=store.now())] if requested is not None else [])],
    )
    store.put(db, "agent_work_packages", work)
    if requested is not None:
        store.event(db, agent["id"], "schedule.budget.expanded", dict(work_package_id=work["id"], before=before, after=target))
    return work["execution_budget"]["tokens"]


def parent_block(store, db, agent, work, requested):
    aid, budget = agent["id"], agent["role"]["budget"]
    window = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()
    goal = store.get(db, "agent_goals", work["goal_id"], aid)
    objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == goal["long_term_objective_id"]), {})
    checks = [("每日", "window=?", (window,), budget["daily_tokens"]), ("目标", "goal_id=?", (goal["id"],), budget["goal_tokens"])]
    if objective.get("stage_planning"):
        checks.append(
            (
                "长期目标",
                "(id IN (SELECT call_id FROM agent_budget_objectives WHERE agent_id=? AND objective_id=?) "
                "OR goal_id IN (SELECT id FROM agent_goals WHERE agent_id=? AND json_extract(body,'$.long_term_objective_id')=?))",
                (aid, objective["id"], aid, objective["id"]),
                objective_limit(agent, objective),
            )
        )
    reasons = []
    for name, where, args, limit in checks:
        used = db.execute(f"SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM agent_budget_calls WHERE agent_id=? AND {where}", (aid, *args)).fetchone()[0]
        if used + requested > limit:
            reasons.append(f"{name} Token 总预算不足：已用及预留 {used}，本次需要 {requested}，上限 {limit}")
    calls = db.execute("SELECT COUNT(*) FROM agent_budget_calls WHERE agent_id=? AND window=?", (aid, window)).fetchone()[0]
    if calls >= budget["max_calls"]:
        reasons.append(f"每日调用次数不足：{calls}/{budget['max_calls']}")
    return "；".join(reasons)
