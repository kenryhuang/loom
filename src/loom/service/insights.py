"""Explain semantic progress with measured costs and explicit missing coverage."""

from collections import Counter


def build_insights(summary, semantic, facts=None):
    reviews = {row["round_id"]: row for row in semantic.get("round_analyses", [])}
    groups, stalls, current = {}, [], None
    for item in summary["rounds"]:
        review = reviews.get(item["id"], {})
        kind = review.get("progress_kind", "unknown")
        group = groups.setdefault(kind, {"rounds": 0, "known_tokens": 0, "unmeasured_rounds": 0})
        tokens = item["usage"].get("total_tokens")
        group["rounds"] += 1
        group["known_tokens"] += tokens or 0
        group["unmeasured_rounds"] += tokens is None
        if kind == "stalled":
            if current is None or current["run_id"] != item["run_id"]:
                current = {"run_id": item["run_id"], "round_ids": [], "known_tokens": 0, "unmeasured_rounds": 0,
                           "evidence_refs": [], "end_reason": "No later reviewed progress"}
                stalls.append(current)
            current["round_ids"].append(item["id"])
            current["known_tokens"] += tokens or 0
            current["unmeasured_rounds"] += tokens is None
            current["evidence_refs"].extend(review.get("evidence_refs", []))
        elif current:
            current["end_reason"] = kind if current["run_id"] == item["run_id"] else "Run boundary"
            current = None
    tasks = summary.get("task_contracts", [])
    superseded = {row["supersedes"] for row in tasks if row.get("supersedes")}
    active = {c["id"] for task in tasks if task["id"] not in superseded for c in task.get("criteria", [])}
    verification = [row for row in semantic.get("verification", []) if row["criterion_id"] in active]
    criteria = [{**criterion, "run_id": task.get("run_id"), "origin": task.get("origin"),
                 "superseded": task["id"] in superseded}
                for task in tasks for criterion in task.get("criteria", [])]
    recovery = []
    for failure in summary.get("failures", []):
        if failure["type"] not in {"run.failed", "llm.failed", "tool.failed", "operation.uncertain"}:
            continue
        later = [r for r in summary["rounds"] if r["run_id"] == failure["run_id"] and r["seq"] > failure["seq"]]
        resolved = next((r for r in later if reviews.get(r["id"], {}).get("progress_kind") == "blocker_resolution"), None)
        window = [r for r in later if resolved is None or r["seq"] <= resolved["seq"]]
        recovery.append({"failure_seq": failure["seq"], "failure_ref": failure["ref"],
                         "status": "candidate_resolution" if resolved else "unresolved_or_unreviewed",
                         "resolution_round_id": resolved["id"] if resolved else None,
                         "rounds": len(window), "known_tokens": sum(r["usage"].get("total_tokens") or 0 for r in window),
                         "unmeasured_rounds": sum(r["usage"].get("total_tokens") is None for r in window),
                         "limitation": "First later reviewed blocker resolution in the same run; causal relation requires diagnosis evidence."})
    contexts = (facts or {}).get("context_deltas", [])
    chars = sum(row.get("content_char_length") or 0 for row in contexts)
    retained = sum(sum(unit["char_length"] for unit in row["retained"]) for row in contexts)
    return {"progress_costs": groups, "stalled_segments": stalls, "recovery_windows": recovery, "criteria": criteria,
            "context_carry": {"message_chars": chars, "retained_chars": retained,
                              "retained_fraction": retained / chars if chars else None,
                              "basis": "Exact message identity within recorded loops; characters, not tokens or waste."},
            "reviewed_rounds": len(reviews), "total_rounds": len(summary["rounds"]),
            "verification_statuses": dict(Counter(row["status"] for row in verification)),
            "verification_basis": "Latest definitions per recorded run and origin; definitions across origins may overlap.",
            "task_completion": "unverified"}
