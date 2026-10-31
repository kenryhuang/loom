"""Durable control outbox: acknowledgement loss cannot abandon pause/cancel."""

import json

from loom.autonomy.store import digest
from loom.service.contracts import ServiceError, canonical


def enqueue(db, aid, cid, sid, kind, goal_id=None):
    value = dict(agent_id=aid, session_id=sid, type=kind, goal_id=goal_id)
    identifier = "agent-control:" + digest([cid, sid, kind])[:40]
    db.execute("INSERT OR IGNORE INTO agent_outbox VALUES(?,?,?,?,?)", (identifier, aid, "control", "pending", canonical(value)))


def drain(service, store, aid):
    with service.lock:
        with store.sessions.transaction() as db:
            rows = db.execute("SELECT id,body FROM agent_outbox WHERE agent_id=? AND kind='control' AND state='pending' ORDER BY rowid", (aid,)).fetchall()
        for row in rows:
            value = json.loads(row["body"])
            sid, kind = value["session_id"], value["type"]
            with store.sessions.transaction() as db:
                agent = store.get(db, "agents", aid)
                link = db.execute("SELECT * FROM agent_sessions WHERE session_id=? AND agent_id=?", (sid, aid)).fetchone()
                if not link or link["taken_over"]:
                    db.execute("UPDATE agent_outbox SET state='superseded' WHERE id=?", (row["id"],))
                    continue
                goal = store.get(db, "agent_goals", value["goal_id"], aid) if value.get("goal_id") else None
                valid = (
                    (goal["state"] == {"stop_run": "cancelled", "pause": "paused", "resume": "active"}[kind])
                    if goal
                    else (agent["state"] == "enabled" if kind == "resume" else agent["state"] in {"paused", "archived"})
                )
                if not valid:
                    db.execute("UPDATE agent_outbox SET state='superseded' WHERE id=?", (row["id"],))
                    continue
                state = service.store._load(db, sid)
                if kind == "resume":
                    binding = state.get("autonomy")
                    bound_goal = store.get(db, "agent_goals", binding["goal_id"], aid) if binding else None
                    if agent["state"] != "enabled" or bound_goal and bound_goal["state"] in {"paused", "cancelled", "superseded"}:
                        db.execute("UPDATE agent_outbox SET state='superseded' WHERE id=?", (row["id"],))
                        continue
            should_send = (
                (kind == "pause" and state["task"]["state"] in {"running", "queued"})
                or (kind == "resume" and state["task"]["state"] == "paused")
                or (kind == "stop_run" and state["task"]["state"] in {"running", "queued", "paused", "awaiting_input", "failed"})
            )
            if should_send:
                try:
                    service.submit(sid, dict(command_id=row["id"], type=kind, payload={}))
                except ServiceError:
                    # Recovery/input must be resolved through the existing protocol before controls can apply.
                    continue
            with store.sessions.transaction() as db:
                db.execute("UPDATE agent_outbox SET state='delivered' WHERE id=?", (row["id"],))
                if kind == "stop_run" and link["work_package_id"]:
                    work = store.get(db, "agent_work_packages", link["work_package_id"], aid)
                    if not work.get("run_ids"):
                        work["state"] = "cancelled"
                        store.put(db, "agent_work_packages", work)
                        db.execute("UPDATE agent_sessions SET work_package_id=NULL WHERE session_id=?", (sid,))
