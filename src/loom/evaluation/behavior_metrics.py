"""Endpoint costs are conditional on cited causal edges; visibility uses recorded injections."""


def endpoint_metrics(facts, graph, assessments=()):
    names = (
        "cost_to_discriminative_evidence",
        "evidence_adoption_lag",
        "cost_to_supported_cause",
        "cause_to_verification",
        "causal_recovery_cost",
        "plan_response_lag",
    )
    result = {name: {"status": "unknown", "value": None, "basis": "No validated causal/visibility endpoints"} for name in names}
    nodes = {n["id"]: n for n in graph["nodes"]}
    from loom.evaluation.behavior_progress import interval_cost

    def line(node):
        return max((r["line_number"] for r in node.get("evidence_refs", [])), default=None)

    def goal_at(line_number, episode_id):
        return max((g for g in facts.get("goal_revisions", []) if g["episode_id"] == episode_id
                    and g.get("applied_line") is not None and g["applied_line"] <= line_number),
                   key=lambda g: g["applied_line"], default=None)

    def cost(start, end, basis, refs):
        ep_id = facts.get("event_episode_ids", {}).get(str(end))
        if ep_id is None or facts.get("event_episode_ids", {}).get(str(start)) != ep_id:
            return {"status": "unknown", "basis": "Missing or cross-episode endpoints"}
        start_goal, end_goal = goal_at(start, ep_id), goal_at(end, ep_id)
        if start_goal and end_goal and start_goal["id"] != end_goal["id"]:
            return {"status": "unknown", "basis": "Endpoints cross an applied goal revision"}
        value = interval_cost(facts, start, end, ep_id)
        if value["status"] == "measured":
            value["status"] = "inferred"
        return {**value, "evidence_refs": refs, "basis": basis, "goal_revision_id": end_goal["id"] if end_goal else None}

    observations = {name: [] for name in names}

    def record(name, value, subject):
        if value.get("status") != "unknown":
            value["subject_id"] = subject
            if value not in observations[name]:
                observations[name].append(value)

    support = {}
    for edge in graph["edges"]:
        a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
        if edge["kind"] == "supports" and a and b and a["kind"] == "evidence" and b["kind"] == "hypothesis":
            endpoint = max(line(a) or 0, line(b) or 0, max((r["line_number"] for r in edge["evidence_refs"]), default=0))
            support[b["id"]] = min(support.get(b["id"], endpoint), endpoint)
    for edge in graph["edges"]:
        a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
        if not a or not b:
            continue
        endpoint = line(a)
        ep_id = facts.get("event_episode_ids", {}).get(str(endpoint))
        episode = next((ep for ep in facts["episodes"] if ep["id"] == ep_id), None)
        if episode is None:
            continue
        active_goal = goal_at(endpoint, ep_id)
        origin = max(episode["start_line"], active_goal["applied_line"] if active_goal else episode["start_line"])
        refs = a["evidence_refs"] + b["evidence_refs"] + edge["evidence_refs"]
        if edge["kind"] == "refutes" and a["kind"] == "evidence":
            record("cost_to_discriminative_evidence", cost(
                origin, endpoint, "Cited evidence eliminating a hypothesis; causal classification is inferred", refs
            ), b["id"])
        if edge["kind"] == "resolves" and a["kind"] == "hypothesis" and b["kind"] == "problem" and a["id"] in support:
            endpoint = support[a["id"]]
            record("cost_to_supported_cause", cost(
                origin, endpoint, "Cited hypothesis resolves the problem; inferred cause, not a proven counterfactual", refs
            ), b["id"])
            record("causal_recovery_cost", cost(line(b), endpoint, "Problem-to-resolution edge; never an unrelated later success", refs), b["id"])
        if edge["kind"] == "verifies" and a["kind"] == "evidence" and b["kind"] == "hypothesis" and b["id"] in support:
            record("cause_to_verification", cost(support[b["id"]], endpoint,
                                                 "Supported cause-to-verification endpoints; oracle scope still applies", refs), b["id"])
        if edge["kind"] == "uses" and a["kind"] == "decision" and b["kind"] == "evidence":
            evidence_lines = {r["line_number"] for r in b["evidence_refs"]}
            visible = [
                injection["ref"]["line_number"]
                for t in facts.get("tool_uses", [])
                if (t.get("raw_output_ref") or {}).get("line_number") in evidence_lines
                for injection in t["injections"]
                if injection.get("raw_output_preserved") and injection.get("source_identity") == "tool_call_id"
            ]
            if visible:
                record("evidence_adoption_lag", cost(
                    min(visible), endpoint, "Recorded full tool-output visibility to cited decision; use relationship inferred", refs
                ), b["id"])
    for goal in facts.get("goal_revisions", []):
        if goal.get("applied_line") is None:
            continue
        segments = {s["id"] for s in facts.get("segments", []) if s.get("goal_revision_id") == goal["id"]}
        reviewed_lines = {
            r["line_number"]
            for a in assessments
            if a["segment_id"] in segments and a["dimension"] == "plan_quality" and a["status"] == "effective"
            for r in a["evidence_refs"]
        }
        plans = [
            p
            for p in facts.get("plan_revisions", [])
            if p["episode_id"] == goal["episode_id"] and p["state"] == "accepted" and p["line"] >= goal["applied_line"] and p["line"] in reviewed_lines
        ]
        if plans:
            plan = min(plans, key=lambda p: p["line"])
            record("plan_response_lag", cost(
                goal["applied_line"],
                plan["line"],
                "Applied revision to accepted plan judged effective for that revision; inferred semantic alignment",
                goal["evidence_refs"] + plan["evidence_refs"],
            ), goal["id"])
    for name, rows in observations.items():
        rows.sort(key=lambda r: (r["end_line"], r["start_line"], r["subject_id"]))
        if rows:
            result[name] = {**rows[0], "observations": rows, "aggregation": "earliest cited endpoint; all per-subject observations retained, not additive"}
    return result
