"""Deterministic containment and archive-order edges, separate from semantic links."""

from loom.evaluation.goal_loops.facts import identity


def process_graph(state):
    nodes, edges = [], []
    for kind in ("goals", "attempts", "phases", "activities"):
        for row in state[kind]:
            if row.get("superseded_by"):
                continue
            nodes.append(dict(id=row["id"], kind="activity" if kind == "activities" else kind[:-1], evidence_refs=row["evidence_refs"]))
            parent = row.get("parent_goal_id") or row.get("goal_id") or row.get("attempt_id")
            if parent:
                edges.append(
                    dict(
                        id="edge:" + identity(parent, row["id"]),
                        source_id=parent,
                        target_id=row["id"],
                        layer="containment",
                        kind="contains",
                        basis="derived",
                        evidence_refs=[],
                    )
                )
    for attempt in state["attempts"]:
        if attempt.get("superseded_by"):
            continue
        phases = sorted((p for p in state["phases"] if p["attempt_id"] == attempt["id"]), key=lambda p: (p["start_line"], p["end_line"]))
        for a, b in zip(phases, phases[1:], strict=False):
            edges.append(
                dict(
                    id="edge:" + identity(a["id"], b["id"]),
                    source_id=a["id"],
                    target_id=b["id"],
                    layer="process",
                    kind="source_order",
                    basis="derived",
                    evidence_refs=[],
                    limitation="Archive order is not causal adoption; phase spans may overlap",
                )
            )

    for goal in state["goals"]:
        if goal.get("parent_goal_id") or goal.get("superseded_by"):
            continue
        identifier = goal["id"] + ":boundary"
        nodes.append(
            dict(
                id=identifier,
                kind="boundary",
                execution_state=goal.get("execution_state", "unknown"),
                goal_disposition=goal.get("goal_disposition", "unknown"),
                end_boundary=goal.get("end_boundary", "snapshot_end"),
                evidence_refs=goal.get("end_refs", []),
            )
        )
        edges.append(
            dict(
                id="edge:" + identity(goal["id"], identifier),
                source_id=goal["id"],
                target_id=identifier,
                layer="process",
                kind="recorded_boundary",
                basis="derived",
                evidence_refs=goal.get("end_refs", []),
            )
        )

    def canonical(identifier):
        seen = set()
        while identifier in state["aliases"] and identifier not in seen:
            seen.add(identifier)
            identifier = state["aliases"][identifier]
        return identifier

    semantic = [{**r, "source_id": canonical(r["source_id"]), "target_id": canonical(r["target_id"])} for r in state["relations"]]
    return dict(nodes=nodes, relations=edges + [r for r in semantic if r["source_id"] != r["target_id"]])
