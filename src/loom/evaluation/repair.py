"""One bounded retry policy for graph and memory, independent of error wording.

Format failures are repaired before evaluating missing coverage. A mixed batch
of format and semantic failures charges both budgets. Exhausted categories never
fall through to another budget. Explicit resume starts a new cycle; the history
of previous cycles remains auditable on the unit.
"""

LIMITS = {"format": 2, "semantic": 1}


def begin_cycle(unit):
    if unit.get("repair_state"):
        unit.setdefault("repair_history", []).append(unit["repair_state"])
    unit["repair_state"] = dict(counts={k: 0 for k in LIMITS}, attempts=0, decisions=[])
    unit.update(repair=0, format_repair=0)  # compatibility for saved consumers


def retry(unit, issues):
    if not issues:
        return False
    state = unit["repair_state"]
    categories = {e["category"] for e in issues}
    active = categories - {"coverage"}
    if not active:
        active = {"semantic"}
    allowed = active <= LIMITS.keys() and all(state["counts"][k] < LIMITS[k] for k in active)
    decision = dict(codes=sorted({e["code"] for e in issues}), categories=sorted(active), retry=allowed)
    state["decisions"].append(decision)
    if allowed:
        state["attempts"] += 1
        for key in active:
            state["counts"][key] += 1
        unit.update(repair=state["counts"]["semantic"], format_repair=state["counts"]["format"])
    return allowed
