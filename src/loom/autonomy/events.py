"""Bound-source event inbox; durable deduplication precedes the existing goal loop."""

from datetime import datetime

from loom.autonomy.contracts import integer, strict
from loom.autonomy.store import digest
from loom.service.contracts import ServiceError, text


def ingest(store, aid, body, *, producer="external_event"):
    strict(body, {"command_id", "source_id", "event_id", "object_id", "version", "occurred_at", "cursor", "origin", "causation", "depth", "type", "summary"})
    for key in ("source_id", "event_id", "object_id", "cursor", "summary"):
        text(body.get(key), key, max_length=4000)
    integer(body.get("version"), "version", 0)
    for key in ("origin", "causation"):
        if body.get(key) is not None:
            text(body[key], key, max_length=4000)
    depth = integer(body.get("depth", 0), "depth", 0, 5)
    try:
        if datetime.fromisoformat(body["occurred_at"]).tzinfo is None:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceError("occurred_at needs a timezone") from exc
    if body.get("type") not in {"changed", "deleted", "revoked"}:
        raise ServiceError("Unknown external event type")
    with store.sessions.transaction() as db:
        request = dict(agent_id=aid, producer=producer, **body)
        replay = store.replay(db, body.get("command_id"), request)
        if replay:
            return replay
        agent = store.get(db, "agents", aid)
        route = next((r for r in agent["role"]["source_routes"] if r["id"] == body["source_id"]), None)
        if not route:
            raise ServiceError("Event source is not bound to this Role", 404)
        identifier = "external_" + digest([aid, body["source_id"], body["event_id"]])[:32]
        prior = db.execute("SELECT body FROM agent_external_events WHERE id=?", (identifier,)).fetchone()
        payload = {k: v for k, v in body.items() if k != "command_id"}
        if prior:
            old = __import__("json").loads(prior[0])
            if old["payload"] != payload:
                raise ServiceError("Event identity reused with different content", 409)
            return store.receipt(db, body["command_id"], request, old)
        previous = [
            e
            for e in store.rows(db, "agent_external_events", aid)
            if e["payload"]["source_id"] == body["source_id"] and e["payload"]["object_id"] == body["object_id"] and e["state"] == "accepted"
        ]
        state = "accepted"
        if previous and body["version"] <= max(e["payload"]["version"] for e in previous):
            state = "out_of_order"
        own = body.get("origin") in {aid, "agent", "agent_report", "method_feedback"}
        if own or depth >= 3:
            state = "suppressed"
        row = dict(id=identifier, agent_id=aid, payload=payload, state=state, received_at=store.now())
        store.put(db, "agent_external_events", row)
        if state == "accepted":
            # Only pending proposals may be withdrawn; running contracts need human review.
            for goal in store.rows(db, "agent_goals", aid):
                if goal.get('stage_id') and body['source_id'] in goal.get('required_source_ids', []) and goal['state'] in {'queued', 'active'}:
                    goal['source_changed'] = identifier
                    store.put(db, 'agent_goals', goal)
                proposal = goal.get("proposal", {})
                if proposal.get("source") == body["source_id"] and proposal.get("object_id") == body["object_id"]:
                    if goal["state"] == "queued":
                        goal.update(state="superseded", revision=goal["revision"] + 1)
                        store.put(db, "agent_goals", goal)
                    elif goal["state"] == "active":
                        goal["source_changed"] = identifier
                        store.put(db, "agent_goals", goal)
            oid = route.get("objective_id")
            if oid:
                observation = dict(
                    id="observation_" + digest(identifier)[:32],
                    agent_id=aid,
                    objective_id=oid,
                    indicator=route["topic"],
                    value=body["summary"],
                    state="unknown",
                    source=route["id"],
                    object_id=body["object_id"],
                    version=body["version"],
                    producer="event_retraction" if body["type"] == "revoked" else producer,
                    role_revision=agent["role_revision"],
                    created_at=store.now(),
                    event_id=identifier,
                )
                store.put(db, "agent_observations", observation)
            agent.update(next_wake_at=store.now(), generation=agent["generation"] + 1)
            store.put(db, "agents", agent)
        store.event(db, aid, "source.event", dict(event_id=identifier, state=state, cursor=body["cursor"]))
        return store.receipt(db, body["command_id"], request, row)
