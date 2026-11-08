"""Daily-only token admission and compatibility with historical budget stops."""

import json
from zoneinfo import ZoneInfo

from loom.service.contracts import ServiceError


def objective_limit(agent, objective):
    """Long-term usage is observable, with no cumulative admission ceiling."""
    return None


def objective_used(db, aid, oid):
    return db.execute(
        "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM agent_budget_calls WHERE agent_id=? AND "
        "(id IN (SELECT call_id FROM agent_budget_objectives WHERE agent_id=? AND objective_id=?) OR "
        "goal_id IN (SELECT id FROM agent_goals WHERE agent_id=? AND json_extract(body,'$.long_term_objective_id')=?))",
        (aid, aid, oid, aid, oid),
    ).fetchone()[0]


def expand_objective(store, db, agent, goal, block):
    """Compatibility entry point: old objective allocations no longer gate work."""
    return None


def execution_block(store, db, agent, work, state):
    """Use a host denial bound to this run; display messages are irrelevant."""
    owner = state.get("autonomy") or {}
    if any(owner.get(k) != v for k, v in dict(agent_id=agent["id"], goal_id=work["goal_id"], work_package_id=work["id"]).items()):
        return None
    run = state.get("run") or {}
    binding = dict(agent_id=agent["id"], goal_id=work["goal_id"], work_package_id=work["id"], run_id=run.get("id"))
    typed = any(code == 'AGENT_BUDGET' for code in (
            (run.get('failure') or {}).get('code'), (state.get('acceptance') or {}).get('cause_code'),
            (work.get('recovery') or {}).get('last_failure', {}).get('error_code')))
    wrapped = (run.get('failure') or {}).get('code') == 'LLM_FAILED'
    if not typed and not wrapped:
        return None
    row = db.execute("SELECT body FROM agent_budget_blocks WHERE id=?", ("execution:" + work["id"],)).fetchone()
    if row:
        saved = json.loads(row[0])
        if not all(saved["binding"].get(k) == v for k, v in binding.items()):
            return None
        # Older streaming adapters erased the error code. Only the host's
        # bound denial is authoritative, never the error prose. A later
        # admitted call invalidates that historical denial.
        if not typed and (not saved.get('at') or db.execute(
            "SELECT 1 FROM agent_budget_calls WHERE agent_id=? AND work_package_id=? "
            "AND category NOT IN ('learning','method_learning') AND created_at>? LIMIT 1",
            (agent['id'], work['id'], saved['at']),
        ).fetchone()):
            return None
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
    """Execution has no cumulative token cap; each call uses the shared day ledger."""
    work['execution_budget'] = dict(work.get('execution_budget') or {}, mode='daily', tokens=None, ceiling=None)
    store.put(db, 'agent_work_packages', work)
    return None


def parent_block(store, db, agent, work, requested):
    aid, budget = agent["id"], agent["role"]["budget"]
    window = store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()
    checks = [("daily", "Daily", "window=?", (window,), budget["daily_tokens"])]
    reasons, scopes = [], []
    for scope, name, where, args, limit in checks:
        used = db.execute(f"SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM agent_budget_calls WHERE agent_id=? AND {where}", (aid, *args)).fetchone()[0]
        if used + requested > limit:
            reasons.append(f"{name} token budget exceeded: used & reserved {used}, this run needs {requested}, limit {limit}")
            scopes.append(dict(scope=scope, limit=limit, used=used, requested=requested, projected=used + requested))
    if not scopes:
        return None
    error = ServiceError('; '.join(reasons), 409, code='EXECUTION_BUDGET')
    error.details = dict(scopes=scopes, window=window)
    return error
