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
    calls = {c["id"]: c for c in facts["model_calls"]}
    ledger = {r["round_id"]: r for r in facts["token_ledger"]}

    def line(node):
        return max((r["line_number"] for r in node.get("evidence_refs", [])), default=None)

    def cost(start, end, basis, refs):
        if start is None or end is None or end < start:
            return {"status": "unknown", "value": None, "basis": "Missing or reversed endpoints"}
        ep_id = facts.get("event_episode_ids", {}).get(str(end))
        ids = [
            c["id"]
            for c in calls.values()
            if c["episode_id"] == ep_id and start <= (c.get("request_ref") or c.get("response_ref") or {}).get("line_number", -1) <= end
        ]
        values = [ledger.get(i, {}).get("total_tokens") for i in ids]
        return {
            "status": "inferred" if all(v is not None for v in values) else "partial",
            "known_tokens": sum(v or 0 for v in values),
            "model_calls": len(ids),
            "round_ids": ids,
            "start_line": start,
            "end_line": end,
            "evidence_refs": refs,
            "basis": basis,
        }

    support = {}
    for edge in graph["edges"]:
        a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
        if edge["kind"] == "supports" and a and b and a["kind"] == "evidence" and b["kind"] == "hypothesis":
            support[b["id"]] = max(line(a) or 0, line(b) or 0, max((r["line_number"] for r in edge["evidence_refs"]), default=0))
    for edge in graph["edges"]:
        a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
        if not a or not b:
            continue
        endpoint = line(a)
        ep_id = facts.get("event_episode_ids", {}).get(str(endpoint))
        episode = next((ep for ep in facts["episodes"] if ep["id"] == ep_id), None)
        if episode is None:
            continue
        refs = a["evidence_refs"] + b["evidence_refs"] + edge["evidence_refs"]
        if edge["kind"] == "refutes" and a["kind"] == "evidence":
            prior = result["cost_to_discriminative_evidence"].get("end_line")
            if prior is None or endpoint < prior:
                result["cost_to_discriminative_evidence"] = cost(
                    episode["start_line"], endpoint, "First cited evidence eliminating a hypothesis; causal classification is inferred", refs
                )
        if edge["kind"] == "resolves" and a["kind"] == "hypothesis" and b["kind"] == "problem" and a["id"] in support:
            endpoint = support[a["id"]]
            result["cost_to_supported_cause"] = cost(
                episode["start_line"], endpoint, "Cited hypothesis resolves the problem; inferred cause, not a proven counterfactual", refs
            )
            result["causal_recovery_cost"] = cost(line(b), endpoint, "Problem-to-resolution edge; never the first unrelated success after a failure", refs)
        if edge["kind"] == "verifies" and a["kind"] == "evidence" and b["kind"] == "hypothesis" and b["id"] in support:
            result["cause_to_verification"] = cost(support[b["id"]], endpoint, "Supported cause-to-verification endpoints; oracle scope still applies", refs)
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
                result["evidence_adoption_lag"] = cost(
                    min(visible), endpoint, "Recorded full tool-output visibility to cited decision; use relationship inferred", refs
                )
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
            result["plan_response_lag"] = cost(
                goal["applied_line"],
                plan["line"],
                "Applied revision to accepted plan judged effective for that revision; inferred semantic alignment",
                goal["evidence_refs"] + plan["evidence_refs"],
            )
    return result
