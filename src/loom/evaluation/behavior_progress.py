"""Merge cited behavior findings and measure intervals without summing overlapping windows."""

from datetime import datetime

from loom.evaluation.behavior_contracts import stable_id
from loom.evaluation.evidence_store import text_value

PROGRESS_KINDS = {"information_gain", "hypothesis_eliminated", "decision", "artifact_change", "blocker_resolution", "verification"}
PLAN_STATUSES = {"followed", "justified_change", "unjustified_deviation", "incomplete", "unknown"}


def unique_refs(refs):
    return list({text_value(r): r for r in refs}.values())


def node_identity(node):
    # Matching labels alone are insufficient; broad event refs may contain several hypotheses.
    return stable_id("node", node["kind"], " ".join(node.get("label", "").split()).casefold(),
                     sorted(text_value(r) for r in node["evidence_refs"]))


def merge_findings(findings):
    grouped = {}
    for d in findings:
        key = (d["pattern_type"], d["mechanism_id"], d.get("intervention_surface"),
               tuple(sorted(text_value(r) for r in d["supporting_refs"])), d["epistemic_status"])
        if key not in grouped:
            grouped[key] = {**d, "dimensions": list(d.get("dimensions", [d["dimension"]])),
                            "segment_ids": [d["segment_id"]], "finding_ids": [d["id"]]}
        else:
            row = grouped[key]
            for plural, singular in (("dimensions", "dimension"), ("segment_ids", "segment_id"), ("finding_ids", "id")):
                if d[singular] not in row[plural]:
                    row[plural].append(d[singular])
            row["counterevidence_refs"] = unique_refs(row.get("counterevidence_refs", []) + d.get("counterevidence_refs", []))
            row["alternatives"] = list(dict.fromkeys(row.get("alternatives", []) + d.get("alternatives", [])))
            row["dimensions"] = list(dict.fromkeys(row["dimensions"] + d.get("dimensions", [])))
    return list(grouped.values())


def interval_cost(facts, start, end, episode_id, *, exclusive_start=False):
    """Calls are owned by request/start position; elapsed span is not active runtime."""
    if start is None or end is None or end < start:
        return {"status": "unknown", "basis": "Missing or reversed endpoints"}
    owner_start = start + int(exclusive_start)
    models = [c for c in facts["model_calls"] if c["episode_id"] == episode_id
              and owner_start <= (c.get("request_ref") or c.get("response_ref") or {}).get("line_number", -1) <= end]
    tools = [t for t in facts.get("tool_uses", [])
             if facts.get("event_episode_ids", {}).get(str((t.get("input_ref") or t.get("raw_output_ref") or {}).get("line_number"))) == episode_id
             and owner_start <= (t.get("input_ref") or t.get("raw_output_ref") or {}).get("line_number", -1) <= end]
    ledger = {r["round_id"]: r for r in facts["token_ledger"]}
    amounts = [ledger.get(c["id"], {}).get("total_tokens") for c in models]
    times = facts.get("event_times", {})
    elapsed = None
    try:
        delta = datetime.fromisoformat(times[str(end)]) - datetime.fromisoformat(times[str(start)])
        if delta.total_seconds() >= 0:
            elapsed = round(delta.total_seconds() * 1000)
    except (ValueError, TypeError, KeyError):
        pass
    return {"status": "measured" if all(v is not None for v in amounts) else "partial", "known_tokens": sum(v or 0 for v in amounts),
            "unmeasured_model_calls": sum(v is None for v in amounts), "model_calls": len(models), "tool_calls": len(tools),
            "round_ids": [c["id"] for c in models], "tool_ids": [t["id"] for t in tools], "elapsed_ms": elapsed,
            "elapsed_basis": "recorded_wall_span_including_waits" if elapsed is not None else "missing_timestamps",
            "start_line": start, "end_line": end, "episode_id": episode_id}


def progress_timeline(facts, progress):
    segments = {s["id"]: s for s in facts["segments"]}
    episodes = {ep["id"]: ep for ep in facts["episodes"]}
    seen, last, result = set(), {}, []
    for row in sorted(progress, key=lambda r: r["line"]):
        s = segments.get(row["segment_id"])
        if not s:
            continue
        key = row["kind"], row["line"], row["label"], s["episode_id"]
        if key in seen:
            continue
        seen.add(key)
        scope = s["episode_id"], s["goal_revision_id"]
        ep = episodes[scope[0]]
        goal = next((g for g in facts.get("goal_revisions", []) if g["id"] == scope[1]), {})
        origin = max(ep["start_line"], goal.get("applied_line") or ep["start_line"])
        # Exclusive previous landmark: adjacent intervals never own the same call twice.
        start = last.get(scope, origin)
        cost = interval_cost(facts, start, row["line"], scope[0], exclusive_start=scope in last)
        result.append({**row, "id": stable_id("progress", *key), "episode_id": scope[0], "goal_revision_id": scope[1],
                       "step_number": s["boundary_key"][-1], "since_previous_progress": cost,
                       "coverage_note": "Between selected, cited landmarks; unreviewed progress may exist"})
        last[scope] = row["line"]
    return result


def merge_graph(graph):
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = {text_value(e): e for e in graph["edges"]}
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}
