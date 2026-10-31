"""Explicitly adopted habits schedule bounded checks inside an existing objective."""

from datetime import datetime

from loom.autonomy.contracts import goal_contract, integer, strict
from loom.autonomy.store import digest
from loom.service.contracts import ServiceError


def configure(store, aid, body):
    strict(body, {"command_id", "entry_id", "expected_revision", "objective_id", "every_seconds"})
    every = integer(body.get("every_seconds"), "every_seconds", 3600, 31536000)
    with store.sessions.transaction() as db:
        request = dict(agent_id=aid, **body)
        prior = store.replay(db, body.get("command_id"), request)
        if prior:
            return prior
        agent = store.get(db, "agents", aid)
        entry = store.get(db, "agent_profile", body.get("entry_id"), aid)
        store.expect(entry, body.get("expected_revision"))
        objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == body.get("objective_id")), None)
        from loom.autonomy.contracts import stage_enabled

        if entry["state"] != "active" or not objective or not (objective.get("iteration") or stage_enabled(objective)):
            raise ServiceError("Adopt the preference and configure objective iteration before scheduling a check", 409)
        entry.update(habit=dict(objective_id=objective["id"], every_seconds=every), revision=entry["revision"] + 1)
        store.put(db, "agent_profile", entry)
        return store.receipt(db, body["command_id"], request, entry)


def materialize(store, aid):
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        if agent["state"] != "enabled":
            return
        from loom.autonomy.profile import effective

        goals = store.rows(db, "agent_goals", aid)
        for entry in effective(store, db, agent):
            habit = entry.get("habit")
            if not habit or any(g.get("habit_id") == entry["id"] and g["state"] not in {"completed", "cancelled"} for g in goals):
                continue
            objective = next((o for o in agent["role"]["long_term_objectives"] if o["id"] == habit["objective_id"]), None)
            from loom.autonomy.contracts import stage_enabled

            if not objective or stage_enabled(objective) or not objective.get("iteration"):
                continue
            window = int((store.clock() - datetime.fromisoformat(agent["created_at"])).total_seconds() // habit["every_seconds"])
            gid = "goal_habit_" + digest([aid, entry["id"], entry["revision"], window])[:32]
            if any(g["id"] == gid for g in goals):
                continue
            goal = dict(
                **goal_contract(
                    dict(
                        objective="Read-only routine check: " + entry["text"],
                        acceptance=objective["iteration"]["acceptance"],
                        long_term_objective_id=objective["id"],
                    ),
                    agent["role"],
                ),
                id=gid,
                agent_id=aid,
                revision=1,
                state="queued",
                created_at=store.now(),
                habit_id=entry["id"],
                read_only=True,
            )
            store.put(db, "agent_goals", goal)
            agent.update(next_wake_at=store.now(), generation=agent["generation"] + 1)
            store.put(db, "agents", agent)
