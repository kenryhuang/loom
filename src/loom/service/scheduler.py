"""FIFO admission with one executor per canonical workspace."""


def ready_sessions(states, active, capacity):
    workspaces = {attempt.workspace for attempt in active.values()}
    slots = capacity - len(active)
    for state in reversed(states):
        workspace = state["task"]["workspace"]
        if slots and state["task"]["state"] == "queued" and state["session_id"] not in active and workspace not in workspaces:
            yield state["session_id"]
            workspaces.add(workspace)
            slots -= 1
