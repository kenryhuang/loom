"""Terminal facts and learning outbox are committed with the Session state."""

import json

from loom.autonomy.store import digest
from loom.service.contracts import canonical


def record_session_state(db, state):
    binding = state.get("autonomy")
    run = state.get("run")
    if not binding or not run or run.get("id") == binding.get("previous_run_id"):
        return
    aid, wid, rid = binding["agent_id"], binding["work_package_id"], run["id"]
    db.execute("INSERT OR IGNORE INTO agent_run_links VALUES(?,?,?,?)", (rid, state["session_id"], aid, wid))
    link = db.execute("SELECT work_package_id FROM agent_run_links WHERE run_id=?", (rid,)).fetchone()
    if link[0] != wid:
        raise ValueError("Execution run already belongs to a different work package")
    row = db.execute("SELECT body FROM agent_work_packages WHERE id=?", (wid,)).fetchone()
    if not row:
        return
    work = json.loads(row[0])
    if rid not in work.setdefault("run_ids", []):
        work["run_ids"].append(rid)
    status = run["state"]
    if status not in {"completed", "failed", "stopped"}:
        if work["state"] not in {"succeeded", "failed", "cancelled"}:
            work["state"] = "running" if status == "running" else "blocked"
            work["blocked_reason"] = state["task"]["state"]
        db.execute("UPDATE agent_work_packages SET body=? WHERE id=?", (canonical(work), wid))
        return
    identifier = "learning_" + digest([state["session_id"], rid])[:32]
    if db.execute("SELECT 1 FROM agent_learning_cycles WHERE id=?", (identifier,)).fetchone():
        return
    cursor = state["event_cursor"]
    gate = db.execute(
        "SELECT json_extract(body,'$.type') FROM events WHERE session_id=? AND seq<=? AND json_extract(body,'$.run_id')=? "
        "AND json_extract(body,'$.type') IN ('acceptance.gate.passed','acceptance.gate.blocked') ORDER BY seq DESC LIMIT 1",
        (state["session_id"], cursor, rid),
    ).fetchone()
    passed = gate is not None and gate[0] == "acceptance.gate.passed"
    work.update(
        state="succeeded" if status == "completed" and passed else ("cancelled" if status == "stopped" else "failed"),
        terminal_run_id=rid,
        outcome=dict(run_state=status, acceptance="passed" if passed else "unknown", event_cursor=cursor, artifacts=state.get("output_artifacts", [])),
    )
    for usage in db.execute("SELECT id,body FROM agent_memory_uses WHERE run_id=?", (rid,)).fetchall():
        body = json.loads(usage["body"])
        body["outcome"] = work["outcome"]
        body["causal_effect"] = "unknown"
        db.execute("UPDATE agent_memory_uses SET body=? WHERE id=?", (canonical(body), usage["id"]))
    db.execute("UPDATE agent_work_packages SET body=? WHERE id=?", (canonical(work), wid))
    cycle = dict(
        id=identifier,
        agent_id=aid,
        goal_id=binding["goal_id"],
        work_package_id=wid,
        session_id=state["session_id"],
        run_id=rid,
        terminal_event_cursor=cursor,
        state="pending",
        phase="snapshot",
        coverage="none",
        memory_result={},
        created_at=work.get("created_at"),
        attempts=0,
    )
    db.execute("INSERT INTO agent_learning_cycles VALUES(?,?,?)", (identifier, aid, canonical(cycle)))
    db.execute("INSERT OR IGNORE INTO agent_outbox VALUES(?,?,?,?,?)", (identifier, aid, "learning", "pending", canonical(cycle)))
    db.execute("UPDATE agent_sessions SET work_package_id=NULL WHERE session_id=? AND work_package_id=?", (state["session_id"], wid))
    row = db.execute("SELECT body FROM agents WHERE id=?", (aid,)).fetchone()
    if row:
        agent = json.loads(row[0])
        agent["next_wake_at"] = ""
        db.execute("UPDATE agents SET body=? WHERE id=?", (canonical(agent), aid))
    goalrow = db.execute("SELECT body FROM agent_goals WHERE id=?", (binding["goal_id"],)).fetchone()
    if goalrow:
        goal = json.loads(goalrow[0])
        packages = [json.loads(r[0]) for r in db.execute("SELECT body FROM agent_work_packages WHERE agent_id=?", (aid,))]
        own = [w for w in packages if w["goal_id"] == goal["id"] and not w.get("superseded_contract")]
        if goal["state"] not in {"cancelled", "paused", "superseded"}:
            goal["state"] = (
                "completed"
                if all(w["state"] == "succeeded" for w in own)
                else ("blocked" if any(w["state"] in {"failed", "cancelled"} for w in own) else "active")
            )
            goal["revision"] += 1
            db.execute("UPDATE agent_goals SET body=? WHERE id=?", (canonical(goal), goal["id"]))
            db.execute("INSERT OR IGNORE INTO agent_revisions VALUES(?,?,?,?)", ("agent_goals", goal["id"], goal["revision"], canonical(goal)))
    seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM agent_events WHERE agent_id=?", (aid,)).fetchone()[0]
    db.execute(
        "INSERT INTO agent_events VALUES(?,?,?)",
        (
            aid,
            seq,
            canonical(
                dict(agent_id=aid, seq=seq, type="work.terminal", payload=dict(work_package_id=wid, learning_cycle_id=identifier, outcome=work["outcome"]))
            ),
        ),
    )
