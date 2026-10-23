"""Read adapter for checkpoints predating structured errors.

Only historical text is interpreted here. New validators and repair policy must
never branch on messages. Unknown legacy failures stay visible, never disappear.
"""

from loom.evaluation.issues import issue

GRAPH_CODES = {
    "Cards need a terminal assessment (reviewed or explicit unknown)": "graph.cards_pending",
    "Stable goal definitions cannot be rewritten; declare a changed objective separately": "graph.goal_definition_immutable",
    "Relationship awaits an accepted action assessment": "graph.assessment_dependency",
    "Relationship requires delivered evidence": "graph.relationship_evidence_required",
}


def graph_issue(error):
    if "code" in error:
        return error
    code = GRAPH_CODES.get(error.get("reason"), "legacy.validation")
    category = "format" if error.get("mechanical") else "semantic"
    if error.get("record_type") == "structure" and "record" not in error:
        code = "graph.structure_pending"
    if code in {"graph.structure_pending", "graph.cards_pending"}:
        category = "coverage"
    coordinates = dict(unit_id=error.get("unit_id", error.get("batch_id")))
    if isinstance(error.get("record"), int):
        coordinates["record_index"] = error["record"] + 1
    return {**issue(code, error.get("reason", "Unclassified legacy error"), category=category, stage="graph", **coordinates), **error}


def legacy_job_state(message):
    return "budget_exhausted" if "budget reached" in message.lower() else "failed"
