"""Checkpointed learning resumes under the shared daily token allowance."""

import json
import re
from copy import deepcopy
from zoneinfo import ZoneInfo

from loom.autonomy.contracts import learning_policy
from loom.autonomy.store import digest


def allowance(db, agent, window):
    """Learning shares the Agent's daily allowance; there is no separate quota."""
    return agent['role']['budget']['daily_tokens']


def current_block(store, job):
    # Only the current typed provider failure can trigger a budget action. Old
    # errors elsewhere in the checkpoint are historical, not current blockers.
    issues = [e for e in job.get("error_details", []) if e.get("provider_code") == "AGENT_BUDGET"]
    if not issues:
        return None
    with store.sessions.transaction() as db:
        row = db.execute("SELECT body FROM agent_budget_blocks WHERE id=?", (job["id"],)).fetchone()
    if row:
        block = json.loads(row[0])
        if any(issue.get("reason") == block["reason"] for issue in issues):
            return block
    # Migration for previously persisted AGENT_BUDGET issues only. Validate the
    # exact old wire format and arithmetic; never classify arbitrary model prose.
    pattern = (r"Agent (learning|daily|goal|work) token budget exhausted: limit=(\d+), used=(\d+) "
               r"\(actual=(\d+), reserved_or_unknown=(\d+)\), requested=(\d+), projected=(\d+), "
               r"remaining=(\d+), exceeded_by=(\d+) tokens; ([^|]*?), category=learning;")
    scopes, windows = [], set()
    for issue in issues:
        for m in re.finditer(pattern, issue.get("reason", "")):
            scope, maximum, used, actual, reserved, requested, projected, remaining, excess, context = m.groups()
            maximum, used, actual, reserved, requested, projected, remaining, excess = map(
                int, (maximum, used, actual, reserved, requested, projected, remaining, excess))
            if used != actual + reserved or projected != used + requested or excess != projected - maximum or remaining != max(0, maximum - used):
                return None
            if window := re.search(r"window=(\d{4}-\d{2}-\d{2})", context):
                windows.add(window[1])
            scopes.append(dict(scope=scope, limit=maximum, used=used, requested=requested, projected=projected))
    if not scopes or len(windows) != 1 or len({s["requested"] for s in scopes}) != 1:
        return None
    block = dict(schema_version=1, binding={**job.get("autonomy_budget", {}), "evaluation_id": job["id"]}, category="learning",
                 window=windows.pop(), requested=scopes[0]["requested"], scopes=scopes,
                 reason=" | ".join(e["reason"] for e in issues), origin="legacy_typed_budget_issue")
    return {**block, "id": digest(block)}


def progress(job):
    cp = job.get("checkpoint", {})
    memory = cp.get("memory", {})
    # Successful work, not paid tokens, timestamps, attempts or repeated failures.
    # Cursors advance when a unit is admitted, before it succeeds. They must
    # not authorize another expansion merely because more work was attempted.
    return digest(dict(graph_revision=cp.get("graph_revision"),
                       units=sorted(k for k, u in cp.get("units", {}).items() if u.get("complete")),
                       memory_units=sorted(k for k, u in memory.get("units", {}).items() if u.get("complete")),
                       records=sorted(k for k, r in memory.get("records", {}).items() if r.get("publication_state", "published") == "published")))


def admit_resume(store, aid, cycle, job, block):
    """Commit an allocation and resume intent atomically before calling Deep Eval.

    Replaying a lost acknowledgement reuses the intent. Every provider request
    still passes BudgetLedger.reserve, including after concurrent consumption.
    """
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        current = store.get(db, "agent_learning_cycles", cycle["id"], aid)
        if current.get("command_generation", 0) != cycle.get("command_generation", 0) or current.get("requested_action") == "cancel":
            return dict(outcome="hold", reason="Learning control changed")
        binding = block.get("binding", {})
        if any(binding.get(k) != current.get(k) for k in ("agent_id", "goal_id", "work_package_id", "evaluation_id")):
            return dict(outcome="hold", reason="Budget failure does not match this learning cycle")
        if agent["state"] != "enabled":
            return dict(outcome="hold", reason="Agent is paused")
        role, budget = agent["role"], agent["role"]["budget"]
        policy = learning_policy(role)
        window = store.clock().astimezone(ZoneInfo(role["timezone"])).date().isoformat()
        retry_round = cycle.get("retry_round", 0)
        resume_key = digest([block["id"], window, retry_round] if retry_round else [block["id"], window])
        resumes = deepcopy(current.get("budget_resumes", []))
        prior = next((r for r in resumes if r["id"] == resume_key), None)
        if prior:
            cycle.update(budget_resumes=resumes, budget_resume_pending=prior)
            return dict(outcome="resume", resume=prior)
        in_window = [r for r in resumes if r.get("window") == window and r.get("retry_round", 0) == retry_round]
        fingerprint = progress(job)
        if in_window and in_window[-1]["progress"] == fingerprint:
            return dict(outcome="stop", reason="LEARNING_NO_PROGRESS: resumed evaluation produced no new completed work")
        if len(in_window) >= policy["max_resumes"]:
            return dict(outcome="stop", reason=f"LEARNING_RETRY_LIMIT: {len(in_window)}/{policy['max_resumes']} automatic resumes")

        def used(where, args):
            return db.execute(f"SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM agent_budget_calls WHERE agent_id=? AND {where}",
                              (aid, *args)).fetchone()[0]

        requested = block["requested"]
        daily = used("window=?", (window,))
        if requested > budget['daily_tokens']:
            return dict(outcome='stop', reason='A single request exceeds the daily token limit; reduce its context or increase daily_tokens')
        if daily + requested > budget['daily_tokens']:
            return dict(outcome='wait', reason=f"Daily budget insufficient: tokens={daily}+{requested}/{budget['daily_tokens']}")
        before = after = budget['daily_tokens']
        resume = dict(id=resume_key, evaluation_id=job["id"], window=window, retry_round=retry_round, block_id=block["id"], progress=fingerprint,
                      before=before, after=after, at=store.now())
        current.update(budget_resumes=[*resumes, resume], budget_resume_pending=resume, state="pending", retry_after="")
        store.put(db, "agent_learning_cycles", current)
        db.execute("UPDATE agent_outbox SET state='pending' WHERE id=?", (cycle["id"],))
        store.event(db, aid, "learning.resume.scheduled", resume)
        cycle.update(budget_resumes=current["budget_resumes"], budget_resume_pending=resume)
        return dict(outcome="resume", resume=resume)
