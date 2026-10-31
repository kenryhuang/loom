"""Deterministic tree membership, separate from evidence-backed judgments."""

from loom.evaluation.framework import root_owned_card as root_owned_card

VERSION = "sequential-attribution.2"


def action_attributions(cp, cards):
    goals = {g["id"]: g for g in cp["entities"]["goals"]}
    roots = sorted((g for g in goals.values() if not g.get("parent_goal_id")), key=lambda g: g["origin_line"], reverse=True)
    ordered = sorted(cards, key=lambda c: (c["start_line"], c["end_line"], c["id"]))
    history, result = [], []

    def scope(card):
        actor = card.get("actor") or {}
        # A trace ID may change every step; loop identifies the executing context.
        return actor.get("loop") or actor.get("trace"), actor.get("role")

    for card in ordered:
        root = next((g for g in roots if g["origin_line"] <= card["start_line"] <= g["end_line"]), None)
        if not root:
            continue
        assessment = cp["assessments"].get(card["id"], {})
        stages = [cp["stages"][sid] for sid in assessment.get("stage_ids", []) if sid in cp["stages"]]
        receipt = root_owned_card(card)
        if receipt:
            rows = [dict(card_id=card["id"], goal_id=root["id"], stage_id=None, basis="framework_rule",
                         rule="required_root_action", anchor_card_id=None, evidence_refs=[])]
        elif stages:
            rows = [dict(card_id=card["id"], goal_id=s["goal_id"], stage_id=s["id"], basis="semantic",
                         rule=None, anchor_card_id=None, evidence_refs=assessment.get("evidence_refs", [])) for s in stages]
        else:
            previous = None
            if not receipt and assessment.get("relation") != "unrelated":
                previous = next((item for item in reversed(history)
                                 if item["root"] == root["id"] and item["card"]["end_line"] <= card["start_line"]
                                 and scope(item["card"]) == scope(card)), None)
            previous_goals = {r["goal_id"] for r in previous["rows"]} if previous else set()
            goal = next(iter(previous_goals)) if len(previous_goals) == 1 else root["id"]
            # The default never supplies evidence, establishes utility or closes a
            # goal. Crossing roots, parallel/overlapping calls or actors is excluded.
            rows = [dict(card_id=card["id"], goal_id=goal, stage_id=None, basis="default_rule",
                         rule="previous_action_same_scope" if len(previous_goals) == 1 else "current_root",
                         anchor_card_id=previous["card"]["id"] if len(previous_goals) == 1 else None,
                         evidence_refs=[])]
        result.extend(rows)
        if card["kind"] in {"model", "tool"} and not receipt:
            history.append(dict(root=root["id"], card=card, rows=rows))
    return result
