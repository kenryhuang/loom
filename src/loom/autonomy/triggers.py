"""Explicit periodic responsibilities coalesce missed windows without model calls."""

from datetime import datetime

from loom.autonomy.contracts import goal_contract
from loom.autonomy.store import digest


def materialize(store, aid):
    with store.sessions.transaction() as db:
        agent = store.get(db, "agents", aid)
        if agent["state"] != "enabled":
            return
        goals = store.rows(db, "agent_goals", aid)
        elapsed = max(0, (store.clock() - datetime.fromisoformat(agent["created_at"])).total_seconds())
        windows = agent.setdefault("schedule_windows", {})
        changed = False
        for objective in agent["role"]["long_term_objectives"]:
            schedule = objective.get("schedule")
            if not schedule:
                continue
            key = f"{agent['role_revision']}:{objective['id']}"
            window = int(elapsed // schedule["every_seconds"])
            if windows.get(key, -1) >= window:
                continue
            # One unresolved responsibility blocks all later windows, including failure.
            if any(g.get("schedule_objective_id") == objective["id"] and g["state"] not in {"completed", "cancelled", "superseded"} for g in goals):
                continue
            gid = "goal_schedule_" + digest([aid, key, window])[:32]
            goal = dict(
                **goal_contract(
                    dict(objective=objective["desired_outcome"], acceptance=schedule["acceptance"], long_term_objective_id=objective["id"]), agent["role"]
                ),
                id=gid,
                agent_id=aid,
                state="queued",
                revision=1,
                created_at=store.now(),
                schedule_objective_id=objective["id"],
                schedule_window=window,
                producer="explicit_schedule",
            )
            store.put(db, "agent_goals", goal)
            store.event(db, aid, "goal.scheduled", dict(goal_id=gid, window=window, missed_windows="coalesced"))
            windows[key] = window
            changed = True
        if changed:
            agent.update(generation=agent["generation"] + 1, next_wake_at=store.now())
            store.put(db, "agents", agent)
