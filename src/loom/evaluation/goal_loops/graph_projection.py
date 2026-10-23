"""Free, deterministic graph/report projections of one committed revision."""

from copy import deepcopy

from loom.evaluation.goal_loops import SCHEMA
from loom.evaluation.goal_loops.attribution import VERSION as ATTRIBUTION_VERSION
from loom.evaluation.goal_loops.attribution import action_attributions
from loom.evaluation.goal_loops.costs import ledger, summarize
from loom.evaluation.goal_loops.facts import run_facts
from loom.evaluation.goal_loops.graph_contract import ANALYZER, PROMPT
from loom.evaluation.goal_loops.graph_order import hierarchy_order, order_goals
from loom.evaluation.goal_loops.graph_state import initialize, occurrences
from loom.evaluation.goal_loops.structure import pending_structure_units


def skeleton(cp, cards, phases, attributions=()):
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
    defaults = {r["card_id"]: r for r in attributions if r["basis"] in {"default_rule", "framework_rule"}}
    for card in cards:
        if card["id"] not in represented:
            assessment = cp["assessments"].get(card["id"])
            nodes.append(
                dict(
                    id="node:" + card["id"],
                    stage_id=None,
                    goal_id=defaults.get(card["id"], {}).get("goal_id"),
                    attribution=defaults.get(card["id"]),
                    kind="framework" if defaults.get(card["id"], {}).get("basis") == "framework_rule" else "unassigned",
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
                    retain_reason="required_root_action" if defaults.get(card["id"], {}).get("basis") == "framework_rule" else "unexplained_activity",
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
    labels = {g["id"]: g.get("display_id", g["id"]) for g in bundle["goals"]}
    for goal in hierarchy_order(bundle["goals"]):
        closure = bundle["closures"].get(goal["id"], {})
        lines += [
            f"- {goal.get('display_id', '')} · {goal['objective']} — claim: {closure.get('claim', 'unknown')}; "
            f"evidence: {closure.get('evidence_state', 'unknown')}; "
            f"disposition: {closure.get('disposition', 'unknown')}"
        ]
        lines += [f"  - Condition: {condition}" for condition in goal["conditions"]]
        if goal.get("parent_goal_id"):
            lines += [f"  - Parent: {labels.get(goal['parent_goal_id'], goal['parent_goal_id'])}; "
                      f"decomposition basis ({goal['parent_relation_basis']}): "
                      f"{goal.get('root_relation') or 'Not recorded; saved nesting is unreviewed'}"]
        if goal.get("first_action_line") is not None:
            lines += [f"  - Activity span: L{goal['first_action_line']}–L{goal['last_action_line']} (may overlap other goals)"]
        if closure:
            lines += [f"  - Closure basis: {closure['reason']}"]
        lines += [f"  - Open: {q}" for q in closure.get("open_questions", [])]
    if c.get("pending_structure_units"):
        lines += ["", f"Goal decomposition pending in {len(c['pending_structure_units'])} windows; reviewed actions alone do not establish a goal tree."]
    for row in bundle.get("structure_reviews", {}).values():
        lines.append(f"- Goal structure: {row['decision']} — {row['reason']}")
    lines += ["", "## Execution skeleton",
              "Ordered by action start; G/S labels identify stable objects, not dependencies. Nesting denotes decomposition, not sequence."]
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
    labels = {g["id"]: g.get("display_id", g["id"]) for g in bundle["goals"]}
    framework = [r for r in bundle.get("action_attributions", []) if r["basis"] == "framework_rule"]
    if framework:
        lines += ["", "## Framework-required root actions"]
        lines += [f"- {r['card_id']} → {labels.get(r['goal_id'], r['goal_id'])}: framework_rule / required_root_action" for r in framework]
    lines += ["", "## Default tree membership", "Sequential defaults are navigation rules, not factual evidence of goal relevance or completion."]
    labels = {g["id"]: g.get("display_id", g["id"]) for g in bundle["goals"]}
    for row in bundle.get("action_attributions", []):
        if row["basis"] == "default_rule":
            lines.append(f"- {row['card_id']} → {labels.get(row['goal_id'], row['goal_id'])}: default_rule / {row['rule']}"
                         + (f"; previous action {row['anchor_card_id']}" if row["anchor_card_id"] else ""))
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
    attributions = action_attributions(cp, cards)
    entities = deepcopy(cp["entities"])
    framework = {r["card_id"]: r for r in attributions if r["basis"] == "framework_rule"}
    # Keep saved semantic judgments for inspection, but framework ownership wins
    # in the execution skeleton and accounting. No LLM judgment is rewritten.
    phases = [p for p in occurrences(cp, cards) if not any(cid in framework for cid in p["card_ids"])]
    entities["activities"] += [dict(
        id="framework:" + c["id"], goal_id=framework[c["id"]]["goal_id"], kind="submission" if c["name"] == "finish" else "verification",
        description=c["name"], call_ids=c["call_ids"], card_ids=[c["id"]], allocation="serves", basis="framework_rule",
        rule="required_root_action", evidence_refs=[], start_line=c["start_line"], end_line=c["end_line"],
    ) for c in cards if c["id"] in framework]
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
    structural_pending = pending_structure_units(cp)
    coverage = dict(
        status="complete" if not pending and not conflicts and not structural_pending else "partial",
        pipeline_complete=not pending and not conflicts and not structural_pending,
        source_events=len(view["records"]),
        source_cards=len(cards),
        reviewed_cards=len(reviewed),
        cards_by_kind={kind: dict(total=sum(c["kind"] == kind for c in cards),
                                  reviewed=sum(c["kind"] == kind and c["id"] in reviewed for c in cards))
                       for kind in ("model", "tool", "boundary")},
        attributed_cards=sum(bool(a["stage_ids"]) for a in cp["assessments"].values()),
        framework_attributed_cards=len(framework),
        default_attributed_cards=sum(r["basis"] == "default_rule" for r in attributions),
        pending_cards=len(pending),
        unknown_cards=sum(a["status"] == "unknown" for a in cp["assessments"].values()),
        pending_card_ids=sorted(pending),
        eligible_calls=len(view["atoms"]),
        assigned_calls=len(assigned),
        unassigned_calls=len(view["atoms"]) - len(assigned),
        reviewed_calls=sum(len(c["call_ids"]) for c in cards if c["id"] in reviewed),
        source_packets=len(cards),
        reviewed_packets=len(reviewed),
        structure_complete=not pending and not structural_pending,
        pending_structure_units=structural_pending,
        validation_issue_count=sum(c.get("record_type") != "structure" or "record" in c for c in conflicts),
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
        structure_reviews=deepcopy(cp.get("structure_reviews", {})),
        protocol_normalizations=deepcopy(cp.get("protocol_normalizations", [])),
        checkpoint_reconciliations=deepcopy(cp.get("checkpoint_reconciliations", [])),
        source=dict(digest=source.source_sha256, run_id=run_id, scope_digest=view["scope_digest"], adapter_version=extraction["version"]),
        analyzer=dict(version=ANALYZER, prompt_version=cp.get("identity", {}).get("prompt", PROMPT), input_policy=cp.get("input_policy")),
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
        stages=deepcopy(list(cp["stages"].values())),
        record_assessments=list(cp["assessments"].values()),
        action_attributions=attributions,
        attribution_version=ATTRIBUTION_VERSION,
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
        skeleton=skeleton(cp, cards, phases, attributions),
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
    order_goals(bundle["goals"], cards, attributions)
    card_order = {c["id"]: (c["start_line"], c["end_line"], c["id"]) for c in cards}
    bundle["record_assessments"] = sorted(bundle["record_assessments"],
                                        key=lambda r: card_order.get(r["card_id"], (float("inf"), 0, r["card_id"])))
    bundle["display_order"] = dict(
        siblings="first_action_line", actions="start_line", nesting="goal_decomposition",
        stage_labels="stable_first_occurrence", sequence_is_dependency=False,
    )
    goal_number = 0
    for goal in bundle["goals"]:
        if goal.get("parent_goal_id"):
            goal_number += 1
            goal["display_id"] = f"G{goal_number}"
        else:
            goal["display_id"] = "Root"
    goal_labels = {g["id"]: g["display_id"] for g in bundle["goals"]}
    stage_labels = {}
    for node in bundle["skeleton"]["nodes"]:
        if node["stage_id"]:
            stage_labels.setdefault(node["stage_id"], f"S{len(stage_labels) + 1}")
    for stage in bundle["stages"]:
        stage["display_id"] = stage_labels.get(stage["id"], "")
    for node in bundle["skeleton"]["nodes"]:
        label = stage_labels.get(node["stage_id"], "Root" if (node.get("attribution") or {}).get("basis") == "framework_rule" else "")
        node.update(display_id=label, goal_display_id=goal_labels.get(node["goal_id"]))
        node["cost"] = summarize([a for a in view["atoms"] if a["id"] in node["call_ids"]])
    bundle["report"] = dict(graph_revision=cp["graph_revision"], status=coverage["status"], format="markdown", text=report(bundle))
    from loom.evaluation.memory.projection import memory_report, project_memory

    bundle["memory"] = project_memory(cp)
    if cp.get("memory_requested"):
        coverage["memory_complete"] = bundle["memory"]["status"] == "complete"
        if not coverage["memory_complete"]:
            coverage.update(status="partial", pipeline_complete=False)
        bundle["report"]["status"] = coverage["status"]
        bundle["report"]["text"] += memory_report(bundle["memory"])
    return bundle
