"""Track goal decomposition separately from action utility coverage."""

from copy import deepcopy

from loom.evaluation.goal_loops.attribution import root_owned_card
from loom.evaluation.goal_loops.graph_contract import TEXT
from loom.evaluation.issues import RuleViolation


def structure_review_context(cp, unit):
    """Show the unresolved obligation and accepted memberships, without rejudging actions."""
    goals = {g["id"]: g for g in cp["entities"]["goals"]}
    eligible = [c for c in cp.get("extraction", {}).get("cards", [])
                if c["id"] in unit["cards"] and c["kind"] in {"model", "tool"} and not root_owned_card(c)]
    memberships = []
    for card in eligible:
        stages = [cp["stages"][sid] for sid in cp["assessments"].get(card["id"], {}).get("stage_ids", []) if sid in cp["stages"]]
        memberships.append(dict(card_id=card["id"], stages=[dict(
            id=s["id"], goal_id=s["goal_id"], parent_goal_id=goals.get(s["goal_id"], {}).get("parent_goal_id")
        ) for s in stages]))
    return dict(
        eligible_card_ids=[c["id"] for c in eligible], accepted_memberships=memberships,
        instruction="Resolve goal decomposition for this window only. Root-stage attributions alone do not resolve it. "
        "If these actions only conclude, coordinate or report the root task, emit structure: single_goal with a specific "
        f"reason (at most {TEXT['maxLength']} characters; put evidence IDs only in refs) and current card field refs. "
        "Existing child goals elsewhere do not prevent a single_goal window. "
        "Otherwise link eligible actions to supported result-oriented child goals. Do not invent subgoals or repeat "
        "accepted assessments. If evidence is insufficient, emit structure: insufficient_evidence and explain the gap.",
    )


def commit_structure(cp, row, *, batch_id, delivered, cutoff, cards, target_ids):
    if not row["reason"].strip():
        raise RuleViolation("graph.structure_reason", "Goal structure decision requires a reason", path=("reason",), category="semantic")
    refs = row["refs"]
    if any(ref not in delivered or delivered[ref]["line"] > cutoff for ref in refs):
        raise RuleViolation("graph.structure_evidence", "Goal structure cites undelivered or future evidence", path=("refs",), category="semantic")
    current = [c for c in cards if c["id"] in target_ids]
    if row["decision"] == "single_goal" and not any(
        c["start_line"] <= delivered[ref]["line"] <= c["end_line"] for ref in refs for c in current
    ):
        raise RuleViolation("graph.structure_current_evidence", "Single-goal decision requires current action evidence", path=("refs",), category="semantic")
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
