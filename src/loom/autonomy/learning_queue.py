"""Durable, per-Agent FIFO of terminal execution runs, independent of work dispatch."""

import json

from loom.autonomy.store import digest
from loom.service.contracts import canonical

TERMINAL = {"completed", "incomplete", "failed", "cancelled"}


def enqueue(db, cycle):
    """Append exactly once; manual retries explicitly append a new queue position."""
    if db.execute("SELECT 1 FROM agent_learning_cycles WHERE id=?", (cycle["id"],)).fetchone():
        return
    started = db.execute("SELECT MIN(rowid) FROM events WHERE session_id=? AND json_extract(body,'$.run_id')=?",
                         (cycle["session_id"], cycle["run_id"])).fetchone()[0]
    cycle["queue_order"] = started if started is not None else next_order(db, cycle["agent_id"])
    db.execute("INSERT INTO agent_learning_cycles VALUES(?,?,?)", (cycle["id"], cycle["agent_id"], canonical(cycle)))
    db.execute("INSERT OR IGNORE INTO agent_outbox VALUES(?,?,?,?,?)", (cycle["id"], cycle["agent_id"], "learning", cycle["state"], canonical(cycle)))


def next_order(db, aid):
    return db.execute("SELECT MAX(COALESCE((SELECT MAX(rowid) FROM events),0), "
                      "COALESCE(MAX(json_extract(body,'$.queue_order')),0))+1 FROM agent_learning_cycles WHERE agent_id=?", (aid,)).fetchone()[0]


def reconcile(store, db, aid):
    """Backfill owned historical runs without changing work/goal outcomes.

    Event row IDs establish execution order across Sessions. The final event for
    each terminal run pins its evidence, including acceptance and failure detail.
    Existing manual evaluations of that exact snapshot are adopted, not duplicated.
    """
    cycles = {c["run_id"]: c for c in store.rows(db, "agent_learning_cycles", aid)}
    runs = db.execute(
        "SELECT l.run_id,l.session_id,l.work_package_id,MIN(e.rowid) AS started,"
        "MAX(CASE WHEN json_extract(e.body,'$.type') IN ('run.failed','run.stopped') "
        "OR (json_extract(e.body,'$.type')='run.state.changed' AND json_extract(e.body,'$.payload.state') "
        "IN ('completed','failed','stopped')) THEN e.seq ELSE 0 END) AS cursor "
        "FROM agent_run_links l JOIN events e ON e.session_id=l.session_id AND json_extract(e.body,'$.run_id')=l.run_id "
        "WHERE l.agent_id=? GROUP BY l.run_id ORDER BY started", (aid,)
    ).fetchall()
    # Upgrade legacy positions together, so historical work creation dates do
    # not reorder successive runs of the same work package.
    if any("queue_order" not in c for c in cycles.values()):
        order = {r["run_id"]: r["started"] for r in runs}
        for i, cycle in enumerate(sorted(cycles.values(), key=lambda c: order.get(c["run_id"], len(runs))), 1):
            cycle.setdefault("queue_order", order.get(cycle["run_id"], i))
            store.put(db, "agent_learning_cycles", cycle)
    for run in runs:
        if run["run_id"] in cycles or not run["cursor"]:
            continue
        # A worker emits run.failed before the host commits the final run state.
        # Enqueuing in that gap would preempt record_session_state's atomic
        # work-outcome/outbox commit and leave the work package running forever.
        live = db.execute("SELECT body FROM sessions WHERE id=?", (run["session_id"],)).fetchone()
        current_run = (json.loads(live[0]).get("run") or {}) if live else {}
        if current_run.get("id") == run["run_id"] and current_run.get("state") not in {"completed", "failed", "stopped"}:
            continue
        work_row = db.execute("SELECT body FROM agent_work_packages WHERE id=? AND agent_id=?", (run["work_package_id"], aid)).fetchone()
        if not work_row:
            continue
        work = json.loads(work_row[0])
        cycle = dict(id="learning_" + digest([run["session_id"], run["run_id"]])[:32], agent_id=aid,
                     goal_id=work["goal_id"], work_package_id=work["id"], session_id=run["session_id"], run_id=run["run_id"],
                     terminal_event_cursor=run["cursor"], state="pending", phase="snapshot", coverage="none",
                     memory_result={}, created_at=store.now(), attempts=0)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_jobs'").fetchone():
            existing = db.execute(
                "SELECT body FROM semantic_jobs WHERE session_id=? AND json_extract(body,'$.run_id')=? "
                "AND json_extract(body,'$.source_cursor')>=? AND json_extract(body,'$.analysis_version')='goal_graph_v1' "
                "AND json_extract(body,'$.settings.extract_memory')=1 ORDER BY rowid DESC LIMIT 1",
                (run["session_id"], run["run_id"], run["cursor"]),
            ).fetchone()
            if existing:
                job = json.loads(existing[0])
                cycle.update(analysis_id=job["analysis_id"], evaluation_id=job["id"], attempts=1, phase="deep_eval",
                             terminal_event_cursor=job["source_cursor"])
        enqueue(db, cycle)
        store.event(db, aid, "learning.enqueued", dict(cycle_id=cycle["id"], run_id=cycle["run_id"], queue_order=cycle["queue_order"]))
    return store.rows(db, "agent_learning_cycles", aid)


def ordered(cycles):
    # A legacy already-running job drains before admitting any queued job.
    return sorted(cycles, key=lambda c: (0 if c["state"] == "running" else 1, c.get("queue_order", 0)))
