"""FIFO admission with shared reads and exclusive resource writes."""


def resource_claims(task):
    if "resource_claims" in task:
        return task["resource_claims"]
    workspace = task.get("workspace")
    return [{"resource_id": f"directory:{workspace}", "access_mode": "exclusive_write"}] if workspace else []


def conflicts(left, right):
    return any(
        a["resource_id"] == b["resource_id"]
        and "none" not in {a["access_mode"], b["access_mode"]}
        and "exclusive_write" in {a["access_mode"], b["access_mode"]}
        for a in left
        for b in right
    )


def ready_sessions(states, active, capacity):
    occupied = []
    for attempt in active.values():
        claims = getattr(attempt, "resource_claims", None)
        occupied.extend(claims if claims else resource_claims({"workspace": attempt.workspace}))
    for state in states:
        if state.get("workspace_blocked"):
            occupied.extend({**claim, "access_mode": "exclusive_write"} for claim in resource_claims(state["task"]))
    slots = capacity - len(active)
    for state in reversed(states):
        claims = resource_claims(state["task"])
        if slots and state["task"]["state"] == "queued" and state["session_id"] not in active and not conflicts(claims, occupied):
            yield state["session_id"]
            occupied.extend(claims)
            slots -= 1
