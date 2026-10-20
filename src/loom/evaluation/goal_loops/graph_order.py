"""Deterministic display order; chronological adjacency never creates ancestry."""


def order_goals(goals, cards, attributions):
    """Annotate copied goals with observed spans and parent basis, then sort them.

    Child activity contributes to a parent's span. Declaration evidence is only
    a fallback: a later judgment can describe an earlier action.
    """
    by_id = {g["id"]: g for g in goals}
    by_card = {c["id"]: c for c in cards}
    spans = {g["id"]: [] for g in goals}
    for row in attributions:
        card = by_card.get(row["card_id"])
        current, seen = row["goal_id"], set()
        while card and current in by_id and current not in seen:
            seen.add(current)
            spans[current].extend((card["start_line"], card["end_line"]))
            current = by_id[current].get("parent_goal_id")
    for goal in goals:
        current, seen = goal.get("parent_goal_id"), {goal["id"]}
        depth = 0
        while current in by_id and current not in seen:
            seen.add(current)
            depth += 1
            current = by_id[current].get("parent_goal_id")
        lines = spans[goal["id"]]
        goal.update(
            first_action_line=min(lines) if lines else None,
            last_action_line=max(lines) if lines else None,
            hierarchy_depth=depth,
            parent_relation_basis=("inferred" if goal.get("root_relation", "").strip() else "unreviewed")
            if goal.get("parent_goal_id") else "root",
        )
    goals.sort(key=lambda g: (
        g["first_action_line"] if g["first_action_line"] is not None else g["origin_line"],
        g["hierarchy_depth"], g["id"],
    ))


def hierarchy_order(goals):
    """Parent before children, siblings in the already established action order."""
    children, roots = {}, []
    ids = {g["id"] for g in goals}
    for goal in goals:
        parent = goal.get("parent_goal_id")
        if parent in ids:
            children.setdefault(parent, []).append(goal)
        else:
            roots.append(goal)
    result, seen = [], set()

    def visit(goal):
        if goal["id"] in seen:
            return
        seen.add(goal["id"])
        result.append(goal)
        for child in children.get(goal["id"], []):
            visit(child)

    for goal in roots + goals:
        visit(goal)
    return result
