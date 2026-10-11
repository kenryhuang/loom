"""Compact navigation and durable page manifests; never discard saved judgments."""

import json


def compact_ref(ref):
    return {k: v for k, v in ref.items() if v is not None}


def compact_segment(segment):
    refs = list({json.dumps(r, sort_keys=True): compact_ref(r) for r in segment["evidence_refs"]}.values())
    return {"id": segment["id"], "episode_id": segment["episode_id"], "goal_revision_id": segment["goal_revision_id"],
            "round_ids": segment["round_ids"], "step_number": segment["boundary_key"][-1],
            "source_lines": [min((r["line_number"] for r in refs), default=None), max((r["line_number"] for r in refs), default=None)],
            "evidence_refs": refs[:8], "source_ref_count": len(refs), "navigation_only": True,
            "navigation": [{"round_id": n["round_id"], "response_excerpt": (n.get("response_excerpt") or "")[:240],
                            "tools": [{**t, "output_excerpt": t.get("output_excerpt", "")[:120]} for t in n.get("tools", [])]}
                           for n in segment.get("navigation", [])]}


def _pages(rows, limit, *, legacy=False):
    pages, page = [], []
    for row in rows:
        if page and len(json.dumps(page + [row], ensure_ascii=legacy)) > limit:
            pages.append(page)
            page = []
        page.append(row)
    if page:
        pages.append(page)
    return pages


def scan_pages(facts, checkpoint, max_prompt_chars):
    compact = {s["id"]: compact_segment(s) for s in facts["segments"]}
    limit = max(1000, max_prompt_chars // 3)
    if "scan_page_ids" not in checkpoint:
        # Pre-manifest checkpoints used large, ASCII-escaped navigation pages. Recover the exact
        # already-scanned prefix before repacking only its unreviewed suffix.
        legacy_index = [{k: s[k] for k in ("id", "episode_id", "goal_revision_id", "evidence_refs", "navigation")}
                        for s in facts["segments"]]
        legacy = _pages(legacy_index, limit, legacy=True)
        stages = checkpoint.get("stages", {})
        saved = 0
        while f"scan:{saved}" in stages:
            saved += 1
        prefix = [[s["id"] for s in page] for page in legacy[:saved]]
        covered = {sid for page in prefix for sid in page}
        remaining = [row for sid, row in compact.items() if sid not in covered]
        # Scan only needs navigation. Actual evidence is read in focus when making judgments.
        for row in remaining:
            row["evidence_refs"] = []
        suffix = [[s["id"] for s in page] for page in _pages(remaining, limit)]
        checkpoint["scan_page_ids"] = prefix + suffix
        checkpoint["scan_index_version"] = "compact-navigation.1"
    pages = []
    for ids in checkpoint["scan_page_ids"]:
        pages.append([{**compact[sid], "evidence_refs": []} for sid in ids])
    return pages


def synthesis_inputs(local, max_prompt_chars):
    fields = ("id", "dimension", "dimensions", "observation", "mechanism", "epistemic_status", "supporting_refs", "counterevidence_refs")
    diagnoses = [{k: d[k] for k in fields if k in d} for d in local["diagnoses"]]
    refs = [r for d in diagnoses for r in d["supporting_refs"][:1]]
    refs += [r for p in local["preserved_behaviors"] for r in p["evidence_refs"][:1]]
    refs = list({json.dumps(r, sort_keys=True): r for r in refs}.values())
    # Re-present concise ranges already validated in focus, not every request/context read.
    quote_limit = min(6000, max_prompt_chars // 5)
    per_quote = min(600, max(1, quote_limit // max(1, len(refs))))
    packets = [{"returned_ref": r, "returned_chars": per_quote} for r in refs[:quote_limit]]
    return {"diagnoses": diagnoses, "preserved_behaviors": local["preserved_behaviors"], "coverage": local["coverage"],
            "evidence_scope": "Bounded excerpts of validated local evidence; synthesize local findings only, never new claims"}, packets


def select_affordable(candidates, prompt_sizes, checkpoint, *, max_segments, max_calls, max_tokens, max_read_rounds):
    """Plan a sample within remaining capacity. Estimates never replace hard usage checks."""
    if "selected" in checkpoint:
        return checkpoint["selected"]
    usage = checkpoint["usage"]
    scan = checkpoint["stage_usage"].get("scan", {})
    rate = max(0.25, scan.get("prompt_tokens", 0) / max(1, scan.get("request_chars", 0)))
    available_tokens = max_tokens - usage["total_tokens"] - min(12000, int(max_tokens * .1))
    available_calls = max_calls - usage["calls"] - 1
    selected, estimated = [], 0
    for sid in candidates[:max_segments]:
        estimate = int((prompt_sizes[sid] * rate + 4096) * (1.3 if max_read_rounds else 1))
        if selected and (estimated + estimate > available_tokens or len(selected) >= available_calls):
            break
        selected.append(sid)
        estimated += estimate
    checkpoint["selection_budget"] = {
        "requested_max_segments": max_segments, "candidate_segments": len(candidates), "selected_segments": len(selected),
        "estimated_focus_tokens": estimated, "available_focus_tokens": max(0, available_tokens),
        "budget_limited": len(selected) < min(max_segments, len(candidates)),
        "basis": "Sample planned from remaining total budget and observed scan token density; unselected segments remain unreviewed",
    }
    return selected
