"""Deterministic window completion obligations, separate from record validity."""

from loom.evaluation.goal_loops.structure import structure_pending
from loom.evaluation.issues import issue


def window_issues(cp, unit):
    context = dict(stage="graph", unit_id=unit["id"], category="coverage", phase="coverage")
    result = []
    if structure_pending(cp, unit):
        result.append(issue("graph.structure_pending",
                            "Subgoal analysis pending: link actions to result-oriented child goals, or explain single_goal with current refs",
                            record_type="structure", **context))
    missing = [cid for cid in unit["cards"] if cid not in cp["assessments"] or cp["assessments"][cid]["status"] == "needs_evidence"]
    if missing:
        result.append(issue("graph.cards_pending", "Cards need a terminal assessment (reviewed or explicit unknown)", card_ids=missing, **context))
    return result
