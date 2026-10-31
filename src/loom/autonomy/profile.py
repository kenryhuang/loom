"""Owner preferences with local exceptions; only explicit human feedback is evidence."""

import json
from datetime import datetime, timedelta

from loom.autonomy.contracts import integer, strict
from loom.autonomy.store import digest
from loom.service.contracts import ServiceError, text


def effective(store, db, agent, *, work_id=None):
    owner = agent["role"].get("owner_id", "local")
    rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM agent_profile")]
    selected = {}
    for row in sorted(rows, key=lambda r: {"owner": 0, "agent": 1, "current_task": 2}[r["scope"]]):
        if row["owner_id"] != owner or row["scope"] != "owner" and row["agent_id"] != agent["id"]:
            continue
        if row["scope"] == "current_task" and row.get("work_package_id") != work_id:
            continue
        # A retired/disputed local exception still masks an owner default.
        selected[row["key"]] = row
    return [r for r in selected.values() if r["state"] == "active" and (r.get("locked_by_user") or r.get("review_at", "") > store.now())]


def feedback(store, aid, body):
    strict(body, {"command_id", "key", "text", "scope", "work_package_id", "session_id", "message_id", "kind", "episode_id", "value", "review_seconds"})
    scope = body.get("scope", "agent")
    kind = body.get("kind", "adopt")
    if scope not in {"owner", "agent", "current_task"} or kind not in {"adopt", "correct", "reject", "explicit"}:
        raise ServiceError("Invalid profile scope or feedback kind")
    key, content = text(body.get("key"), "key", max_length=100), text(body.get("text"), "text", max_length=4000)
    review = integer(body.get("review_seconds", 2592000), "review_seconds", 60, 31536000)
    with store.sessions.transaction() as db:
        request = dict(agent_id=aid, **body)
        replay = store.replay(db, body.get("command_id"), request)
        if replay:
            return replay
        agent = store.get(db, "agents", aid)
        if body.get("session_id"):
            link = db.execute("SELECT agent_id FROM agent_sessions WHERE session_id=?", (body["session_id"],)).fetchone()
            if not link or link[0] != aid:
                raise ServiceError("Feedback Session outside Agent scope", 404)
            state = store.sessions._load(db, body["session_id"])
            message = next((m for m in state["messages"] if m["id"] == body.get("message_id")), None)
            if not message or message.get("producer") != "human":
                raise ServiceError("Preference feedback requires a recorded human message")
            # Host pins the real episode; caller cannot manufacture independent episodes.
            revisions = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM events WHERE session_id=? AND seq>=? AND json_extract(body,'$.type')='task.goal.revised' ORDER BY seq",
                    (body["session_id"], message["seq"]),
                )
            ]
            episode_id = next(
                (r["payload"].get("task_episode_id") for r in revisions if message["command_id"] in r["payload"].get("applied_command_ids", [])), None
            )
            episode = f"{body['session_id']}:{episode_id or 'session'}"
            source = dict(session_id=body["session_id"], message_id=message["id"], producer="human")
        else:
            if kind != "explicit":
                raise ServiceError("Inferred preferences require independent recorded human feedback")
            episode = "explicit:" + body["command_id"]
            source = dict(command_id=body["command_id"], producer="human")
        work_id = body.get("work_package_id")
        if scope == "current_task":
            store.get(db, "agent_work_packages", work_id, aid)
        owner = agent["role"].get("owner_id", "local")
        identifier = "profile_" + digest([owner if scope == "owner" else aid, scope, work_id, key])[:32]
        saved = db.execute("SELECT body FROM agent_profile WHERE id=?", (identifier,)).fetchone()
        row = (
            json.loads(saved[0])
            if saved
            else dict(
                id=identifier,
                agent_id=aid,
                owner_id=owner,
                key=key,
                text=content,
                scope=scope,
                work_package_id=work_id,
                state="candidate",
                revision=0,
                evidence=[],
                counterexamples=[],
                locked_by_user=False,
            )
        )
        evidence = dict(episode_id=episode, source=source, text=content, kind=kind, at=store.now())
        if not any(e["episode_id"] == episode and e["kind"] == kind for e in row["evidence"]):
            row["evidence"].append(evidence)
        if kind == "reject" or content != row["text"]:
            row["counterexamples"].append(evidence)
            if not row["locked_by_user"] and row["state"] != "retired":
                row["state"] = "disputed"
        if kind == "explicit" and not row["locked_by_user"]:
            row.update(text=content, state="active", evidence_state="human_asserted")
        # Repeated feedback creates a reviewable candidate, never external authority.
        row.update(
            independent_episodes=len({e["episode_id"] for e in row["evidence"]}),
            revision=row["revision"] + 1,
            review_at=(store.clock() + timedelta(seconds=review)).isoformat(),
        )
        store.put(db, "agent_profile", row)
        changed(store, db, agent, row)
        return store.receipt(db, body["command_id"], request, row)


def changed(store, db, agent, row):
    impacted = [agent]
    if row["scope"] == "owner":
        impacted = [json.loads(r[0]) for r in db.execute("SELECT body FROM agents") if json.loads(r[0])["role"].get("owner_id", "local") == row["owner_id"]]
    for item in impacted:
        item.update(guidance_version=item["guidance_version"] + 1, generation=item["generation"] + 1, next_wake_at=store.now())
        store.put(db, "agents", item)
        store.event(db, item["id"], "profile.changed", dict(entry_id=row["id"], state=row["state"], revision=row["revision"]))


def command(store, aid, body):
    strict(body, {"command_id", "entry_id", "expected_revision", "type", "work_package_id"})
    with store.sessions.transaction() as db:
        request = dict(agent_id=aid, **body)
        replay = store.replay(db, body.get("command_id"), request)
        if replay:
            return replay
        agent = store.get(db, "agents", aid)
        row = store.get(db, "agent_profile", body.get("entry_id"))
        if row["agent_id"] != aid and not (row["scope"] == "owner" and row["owner_id"] == agent["role"].get("owner_id", "local")):
            raise ServiceError("Profile outside owner scope", 404)
        store.expect(row, body.get("expected_revision"))
        action = body.get("type")
        if action == "lock":
            row["locked_by_user"] = True
        elif action == "unlock":
            row["locked_by_user"] = False
        elif action in {"retire", "restore", "activate", "trial", "once"}:
            if action == "trial" and (row["independent_episodes"] < 2 or row["counterexamples"]):
                raise ServiceError("Trial requires two independent episodes without unresolved counterexamples", 409)
            if action == "once":
                work = store.get(db, "agent_work_packages", body.get("work_package_id"), aid)
                row = {
                    **row,
                    "id": "profile_" + digest([row["id"], work["id"]])[:32],
                    "agent_id": aid,
                    "scope": "current_task",
                    "work_package_id": work["id"],
                    "revision": 0,
                }
            row["state"] = "retired" if action == "retire" else "active"
            row["trial"] = action == "trial"
            row["review_at"] = (store.clock() + timedelta(days=7 if action == "trial" else 30)).isoformat()
        else:
            raise ServiceError("Unknown profile action")
        row["revision"] += 1
        store.put(db, "agent_profile", row)
        changed(store, db, agent, row)
        return store.receipt(db, body["command_id"], request, row)


def review_due(store, aid):
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        for row in store.rows(db, "agent_profile", aid):
            if row["state"] == "active" and not row.get("locked_by_user") and datetime.fromisoformat(row["review_at"]) <= store.clock():
                row.update(state="candidate", revision=row["revision"] + 1, review_reason="Review expired preference or reversible trial")
                store.put(db, "agent_profile", row)
                changed(store, db, agent, row)


def scan_feedback(store, aid):
    """Conservative literal candidates; semantic adoption still requires review.

    Only explicit future-oriented human messages qualify. A Session contributes
    at most one independent episode until a stronger episode identity exists.
    """
    import re

    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        if agent["state"] != "enabled":
            return
        cursors = dict(agent.get("profile_scan_cursors", {}))
        batch = []
        for row in db.execute("SELECT session_id FROM agent_sessions WHERE agent_id=?", (aid,)):
            try:
                session = store.sessions._load(db, row[0])
            except ServiceError:
                continue
            for message in session["messages"]:
                if message.get("seq", 0) > cursors.get(row[0], 0):
                    batch.append((row[0], message))
                    if len(batch) >= 100:
                        break
            if len(batch) >= 100:
                break
    for sid, message in batch:
        content = message.get("content", "").strip()
        future = re.match(r"^(以后|今后|长期|请一直|always\b|in future\b|from now on\b)", content, re.IGNORECASE)
        if message.get("producer") == "human" and message.get("role") == "user" and future and 1 <= len(content) <= 4000:
            normalized = " ".join(content.split())
            feedback(
                store,
                aid,
                dict(
                    command_id="profile-scan:" + digest([aid, sid, message["id"]]),
                    key="observed-" + digest(normalized)[:20],
                    text=normalized,
                    scope="agent",
                    kind="adopt",
                    session_id=sid,
                    message_id=message["id"],
                ),
            )
        cursors[sid] = max(cursors.get(sid, 0), message.get("seq", 0))
    if batch:
        with store.sessions.transaction() as db:
            latest = store.get(db, "agents", aid)
            latest["profile_scan_cursors"] = cursors
            store.put(db, "agents", latest)
