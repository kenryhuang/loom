"""Track goal decomposition separately from action utility coverage."""

from copy import deepcopy

from loom.evaluation.goal_loops.attribution import root_owned_card


def commit_structure(cp, row, *, batch_id, delivered, cutoff, cards, target_ids):
    if not row["reason"].strip():
        raise ValueError("Goal structure decision requires a reason")
    refs = row["refs"]
    if any(ref not in delivered or delivered[ref]["line"] > cutoff for ref in refs):
        raise ValueError("Goal structure cites undelivered or future evidence")
    current = [c for c in cards if c["id"] in target_ids]
    if row["decision"] == "single_goal" and not any(
        c["start_line"] <= delivered[ref]["line"] <= c["end_line"] for ref in refs for c in current
    ):
        raise ValueError("Single-goal decision requires current action evidence")
    cp.setdefault("structure_reviews", {})[batch_id] = dict(
        **deepcopy(row), card_ids=sorted(target_ids), knowledge_cutoff=cutoff,
        evidence_refs=[deepcopy(delivered[ref]["ref"]) for ref in refs],
    )


def structure_pending(cp, unit):
    """Root-only phases cannot silently count as successful goal decomposition.

    A supported child-goal membership demonstrates decomposition in this window;
    otherwise the judge must explicitly decide that this is a single-goal window.
    Boundaries, harness verification and task termination carry no independent
    decomposition obligation; their root membership is recorded separately.
    """
    cards = cp.get("extraction", {}).get("cards", [])
    eligible = {c["id"] for c in cards if c["id"] in unit["cards"] and c["kind"] in {"model", "tool"} and not root_owned_card(c)}
    if not eligible:
        return False
    children = {g["id"] for g in cp["entities"]["goals"] if g.get("parent_goal_id")}
    for cid in eligible:
        for sid in cp["assessments"].get(cid, {}).get("stage_ids", []):
            if cp["stages"].get(sid, {}).get("goal_id") in children:
                return False
    decision = cp.get("structure_reviews", {}).get(unit["id"], {})
    return not (decision.get("decision") == "single_goal" and set(decision.get("card_ids", [])) == set(unit["cards"]))


def pending_structure_units(cp):
    return [u["id"] for u in cp.get("units", {}).values() if structure_pending(cp, u)]
