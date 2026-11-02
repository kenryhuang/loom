"""One-time migration of pre-typed host budget failures; never used by dispatch."""

import re


def migrate_budget_failure(db, agent, work, state, goal):
    run = state.get("run") or {}
    failure = run.get("failure") or {}
    reason = run.get("reason") or failure.get("message") or ""
    binding = dict(agent_id=agent["id"], goal_id=work["goal_id"], work_package_id=work["id"], run_id=run.get("id"))
    if db.execute("SELECT 1 FROM agent_budget_blocks WHERE id=?", ("execution:" + work["id"],)).fetchone():
        return
    if failure.get("code") not in {"LLM_FAILED", "AGENT_BUDGET"} or reason != failure.get("message"):
        return None
    pattern = (r"Agent (objective|work|goal|daily) token budget exhausted: limit=(\d+), used=(\d+) "
               r"\(actual=(\d+), reserved_or_unknown=(\d+)\), requested=(\d+), projected=(\d+), "
               r"remaining=(\d+), exceeded_by=(\d+) tokens; ([^;]+), category=execution; increase \w+_tokens "
               r"(?:\(cumulative budget; does not reset daily\)|or wait for the next daily window)")
    scopes = []
    for part in reason.split(" | "):
        match = re.fullmatch(pattern, part)
        if not match:
            return None
        scope, *values, context = match.groups()
        limit, used, actual, reserved, requested, projected, remaining, excess = map(int, values)
        if (used != actual + reserved or projected != used + requested or excess != projected - limit or excess <= 0
                or requested <= 0 or remaining != max(0, limit - used)):
            return None
        expected = dict(objective="objective_id=" + goal["long_term_objective_id"], work="work_package_id=" + work["id"], goal="goal_id=" + goal["id"])
        if scope in expected and context != expected[scope]:
            return None
        if scope == "daily" and not re.fullmatch(r"window=\d{4}-\d{2}-\d{2}, timezone=" + re.escape(agent["role"]["timezone"]), context):
            return None
        scopes.append(dict(scope=scope, limit=limit, used=used, requested=requested, projected=projected))
    if not scopes or len({s["requested"] for s in scopes}) != 1:
        return None
    from loom.autonomy.store import digest

    block = dict(id="legacy:" + digest([binding, reason]), binding=binding, category="execution", reason=reason,
                 requested=scopes[0]["requested"], scopes=scopes, origin="legacy_host_budget_failure", at=run.get("finished_at", ""))
    from loom.service.contracts import canonical

    db.execute("INSERT INTO agent_budget_blocks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
               ("execution:" + work["id"], agent["id"], canonical(block)))
    return block

