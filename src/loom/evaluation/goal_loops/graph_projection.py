"""Free, deterministic graph/report projections of one committed revision."""

from copy import deepcopy

from loom.evaluation.goal_loops import SCHEMA
from loom.evaluation.goal_loops.costs import ledger, summarize
from loom.evaluation.goal_loops.facts import run_facts
from loom.evaluation.goal_loops.graph_contract import ANALYZER, PROMPT
from loom.evaluation.goal_loops.graph_state import initialize, occurrences


def skeleton(cp, cards, phases):
    nodes, membership = [], {}
    order = {c["id"]: n for n, c in enumerate(cards)}
    endpoints = {r[k] for r in cp["relations"] for k in ("source_id", "target_id")}
    critical = {"decision", "artifact", "verification", "handoff"}
    for phase in phases:
        previous = nodes[-1] if nodes else None
        retain = phase["id"] in endpoints or phase["contribution"] in critical or phase["status"] in ("failed", "cancelled")
        if (
            previous
            and not retain
            and not previous["boundary"]
            and previous["stage_id"] == phase["stage_id"]
            and previous["end_line"] <= phase["start_line"]
            and order[previous["card_ids"][-1]] + 1 == order[phase["card_ids"][0]]
        ):
            node = previous
            node["member_ids"].append(phase["id"])
            node["card_ids"] += phase["card_ids"]
            node["call_ids"] = list(dict.fromkeys(node["call_ids"] + phase["call_ids"]))
            node["key_delta"] = phase["delta"] or node["key_delta"]
            node["end_line"] = phase["end_line"]
            node["evidence_refs"] += [r for r in phase["evidence_refs"] if r not in node["evidence_refs"]]
        else:
            stage = cp["stages"][phase["stage_id"]]
            node = dict(
                id="node:" + phase["id"],
                stage_id=stage["id"],
                goal_id=stage["goal_id"],
                objective=stage["title"],
                kind=stage["kind"],
                member_ids=[phase["id"]],
                card_ids=phase["card_ids"],
                call_ids=phase["call_ids"],
                key_delta=phase["delta"],
                outcome_or_gap="Closure not established at this point",
                start_line=phase["start_line"],
                end_line=phase["end_line"],
                evidence_refs=deepcopy(phase["evidence_refs"]),
                boundary=retain,
                retain_reason="critical_change_or_dependency" if retain else "contiguous_work",
            )
            nodes.append(node)
        membership[phase["id"]] = node["id"]
    represented = {c for n in nodes for c in n["card_ids"]}
    for card in cards:
        if card["id"] not in represented:
            assessment = cp["assessments"].get(card["id"])
            nodes.append(
                dict(
                    id="node:" + card["id"],
                    stage_id=None,
                    goal_id=None,
                    kind="unassigned",
                    objective=card["name"],
                    member_ids=[],
                    card_ids=[card["id"]],
                    call_ids=card["call_ids"],
                    key_delta=assessment["delta"] if assessment else "Pending semantic analysis",
                    outcome_or_gap=assessment["status"] if assessment else "pending",
                    start_line=card["start_line"],
                    end_line=card["end_line"],
                    evidence_refs=assessment.get("evidence_refs", []) if assessment else [],
                    boundary=True,
                    retain_reason="unexplained_activity",
                )
            )
    nodes.sort(key=lambda n: (n["start_line"], n["end_line"], n["id"]))
    for node in nodes:
        closure = cp["closures"].get(node["goal_id"], {})
        known = [r for r in closure.get("history", []) + [closure] if r and r["knowledge_cutoff"] <= node["end_line"]]
        if known:
            node["outcome_or_gap"] = known[-1]["reason"]
    edges = []
    for relation in cp["relations"]:
        a, b = membership.get(relation["source_id"]), membership.get(relation["target_id"])
        if a and b and a != b:
            edges.append({**relation, "source_id": a, "target_id": b, "relation_ids": [relation["id"]]})
    return dict(
        graph_revision=cp["graph_revision"],
        projection_version="goal-skeleton-projection.1",
        nodes=nodes,
        edges=edges,
        ordering="source order is layout only, not causation",
        granularity="contiguous stable stages; critical changes retained",
    )


def report(bundle):
    c = bundle["coverage"]
    lines = [
        "# Goal process analysis",
        f"Source: {bundle['source']['digest']}",
        f"Run: {bundle['source']['run_id']} · Revision: {bundle['graph_revision']} · {c['status']}",
        "",
        "## Goals",
    ]
    for goal in bundle["goals"]:
        closure = bundle["closures"].get(goal["id"], {})
        lines += [
            f"- {goal.get('display_id', '')} · {goal['objective']} — claim: {closure.get('claim', 'unknown')}; "
            f"evidence: {closure.get('evidence_state', 'unknown')}; "
            f"disposition: {closure.get('disposition', 'unknown')}"
        ]
        lines += [f"  - Condition: {condition}" for condition in goal["conditions"]]
        if closure:
            lines += [f"  - Closure basis: {closure['reason']}"]
        lines += [f"  - Open: {q}" for q in closure.get("open_questions", [])]
    lines += ["", "## Execution skeleton"]
    for node in bundle["skeleton"]["nodes"]:
        lines += [
            f"- {node.get('display_id', '')} · {node['objective']} · {node.get('goal_display_id') or 'Unassigned'} "
            f"(L{node['start_line']}–{node['end_line']}, {len(node['call_ids'])} calls): {node['key_delta']}",
            f"  - {node['outcome_or_gap']}",
        ]
        if node.get("cost"):
            cost = node["cost"]
            lines += [
                f"  - Recorded tokens: {cost['total_tokens']['known_sum']}; busy union ms: {cost['observed_busy_union_ms']}; "
                "related calls, not additive across shared nodes."
            ]
    lines += ["", "## Action contributions"]
    for row in bundle["record_assessments"]:
        lines += [f"- {row['card_id']} · {row['status']} · {row['contribution']}: {row['delta']}"]
        lines += [
            f"  - Purpose: {row['purpose']}; expected: {row['expected']}; necessity: {row['necessity']}; redundancy: {row['redundancy']}.",
            "  - Evidence: " + ", ".join(f"L{r['line_number']}:{r.get('field_path', '')}" for r in row["evidence_refs"]),
        ]
    lines += [
        "",
        "## Evidence visibility",
        "Captured output, presented context and semantic adoption are separate; previews do not establish full reading.",
        "",
        "## Diagnostics",
    ]
    lines += [f"- {d['observation']} — {d['interpretation']} Alternative: {d['alternative']} Improvement: {d['improvement']}" for d in bundle["diagnostics"]]
    lines += ["", "## Authoritative acceptance", "Recorded verifier and host decisions; not independent command replay."]
    lines += [f"- L{v['line']} {v['type']}" for v in bundle["facts"]["verification"]]
    lines += [
        "",
        "## Coverage",
        f"Cards reviewed: {c['reviewed_cards']}/{c['source_cards']}; unknown: {c['unknown_cards']}; pending: {c['pending_cards']}.",
        "Summary projection, not a complete evidence package. Original source references are in the JSON bundle.",
    ]
    return "\n".join(lines)


def project(source, facts, run_id, checkpoint, extraction=None):
    from loom.evaluation.goal_loops.cards import compile_cards
    from loom.evaluation.goal_loops.execution_view import execution_view

    view = run_facts(source, facts, run_id)
    cp = checkpoint if checkpoint.get("entities") else {**initialize(view), **checkpoint}
    extraction = extraction or cp.get("extraction") or compile_cards(source, facts, view)
    cards = extraction["cards"]
    entities = deepcopy(cp["entities"])
    phases = occurrences(cp, cards)
    entities.update(phases=phases, relations=deepcopy(cp["relations"]), diagnostics=deepcopy(cp["diagnostics"]))
    for goal in entities["goals"]:
        recorded = view["statistics"].get("execution_status", "unknown") if not goal.get("parent_goal_id") else "unknown"
        goal.update(
            execution_state="ended" if recorded in ("completed", "failed", "cancelled", "stopped") else "unknown",
            end_boundary="explicit" if recorded in ("completed", "failed", "cancelled", "stopped") else "snapshot_end",
            verification=[v for v in view["verification"] if goal["origin_line"] <= v["line"] <= goal["end_line"]],
        )
    accounting = ledger(view, entities)
    assigned = {r["cost_atom_id"] for r in accounting["assignments"] if r["owner"] != "analysis:unassigned"}
    reviewed = {c for c, a in cp["assessments"].items() if a["status"] in ("reviewed", "unknown")}
    pending = {c["id"] for c in cards} - reviewed
    conflicts = [c for c in cp["conflicts"] if not c.get("resolved")]
    coverage = dict(
        status="complete" if not pending and not conflicts else "partial",
        pipeline_complete=not pending and not conflicts,
        source_events=len(view["records"]),
        source_cards=len(cards),
        reviewed_cards=len(reviewed),
        cards_by_kind={kind: dict(total=sum(c["kind"] == kind for c in cards),
                                  reviewed=sum(c["kind"] == kind and c["id"] in reviewed for c in cards))
                       for kind in ("model", "tool", "boundary")},
        attributed_cards=sum(bool(a["stage_ids"]) for a in cp["assessments"].values()),
        pending_cards=len(pending),
        unknown_cards=sum(a["status"] == "unknown" for a in cp["assessments"].values()),
        pending_card_ids=sorted(pending),
        eligible_calls=len(view["atoms"]),
        assigned_calls=len(assigned),
        unassigned_calls=len(view["atoms"]) - len(assigned),
        reviewed_calls=sum(len(c["call_ids"]) for c in cards if c["id"] in reviewed),
        source_packets=len(cards),
        reviewed_packets=len(reviewed),
        structure_complete=not pending,
        reconciliation_complete=not conflicts,
        diagnosed_goals=list({d["goal_id"] for d in cp["diagnostics"]}),
        diagnostic_scope="Only cited findings; unreported goals are not declared efficient",
        review_gaps=conflicts,
        limitations=view["limitations"] + ["Card review does not establish full raw-output review", "Semantic completeness is separate from task acceptance"],
    )
    for d in entities["diagnostics"]:
        d["involved_cost"] = summarize([a for a in view["atoms"] if a["id"] in d["call_ids"]])
    bundle = dict(
        schema_version=SCHEMA,
        graph_revision=cp["graph_revision"],
        source=dict(digest=source.source_sha256, run_id=run_id, scope_digest=view["scope_digest"], adapter_version=extraction["version"]),
        analyzer=dict(version=ANALYZER, prompt_version=PROMPT),
        **entities,
        facts=dict(events=view["records"], verification=view["verification"], statistics=view["statistics"]),
        extraction=dict(
            version=extraction["version"],
            cards=cards,
            record_index=extraction["record_index"],
            metadata_links=extraction.get("metadata_links", []),
            registry=extraction["registry"],
            fetched_ranges=cp["reviewed_ranges"],
            delivered_ranges=cp.get("delivered_ranges", []),
        ),
        stages=list(cp["stages"].values()),
        record_assessments=list(cp["assessments"].values()),
        closures=cp["closures"],
        criterion_coverage=[
            dict(
                goal_id=g["id"],
                conditions=g["conditions"],
                closure=cp["closures"].get(g["id"]),
                verification_refs=[r for v in g["verification"] for r in v["evidence_refs"]],
            )
            for g in entities["goals"]
            if not g.get("parent_goal_id")
        ],
        skeleton=skeleton(cp, cards, phases),
        graph=dict(nodes=[dict(id=p["id"], kind="phase") for p in phases], relations=cp["relations"]),
        cost_ledger=accounting,
        coverage=coverage,
        evaluator_usage={
            **cp["usage"],
            "reservations": cp["reservations"],
            "input_cost": dict(
                request_count=len(cp["reservations"]),
                total_chars=sum(r.get("input_profile", {}).get("total_chars", 0) for r in cp["reservations"]),
                max_request_chars=max((r.get("input_profile", {}).get("total_chars", 0) for r in cp["reservations"]), default=0),
                estimation="Request characters include native response schema; token estimates are conservative, not billed usage",
            ),
        },
    )
    bundle["execution_view"] = execution_view(source, bundle)
    goal_number = 0
    for goal in bundle["goals"]:
        if goal.get("parent_goal_id"):
            goal_number += 1
            goal["display_id"] = f"G{goal_number}"
        else:
            goal["display_id"] = "Root"
    goal_labels = {g["id"]: g["display_id"] for g in bundle["goals"]}
    for index, node in enumerate(bundle["skeleton"]["nodes"], 1):
        node.update(display_id=f"S{index}", goal_display_id=goal_labels.get(node["goal_id"]))
        node["cost"] = summarize([a for a in view["atoms"] if a["id"] in node["call_ids"]])
    bundle["report"] = dict(graph_revision=cp["graph_revision"], status=coverage["status"], format="markdown", text=report(bundle))
    return bundle
