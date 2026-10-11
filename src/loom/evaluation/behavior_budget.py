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
    """Bound summary input independently of saved local judgments and their full provenance."""
    fields = ('diagnoses', 'assessments', 'progress', 'plan_assessments', 'fact_assessments', 'preserved_behaviors')
    keep = {'id', 'dimension', 'dimensions', 'segment_id', 'status', 'epistemic_status', 'observation', 'mechanism',
            'rationale', 'kind', 'label', 'line', 'plan_snapshot_id', 'node_id', 'fact_id', 'fact_kind', 'fact_label',
            'fact_line', 'fact_ids', 'unknown_reason', 'behavior'}
    groups = {}
    for field in fields:
        groups[field] = []
        for row in local.get(field, []):
            compact = {k: (v[:600] if isinstance(v, str) else v) for k, v in row.items() if k in keep}
            compact['source_lines'] = sorted({r['line_number'] for name in ('evidence_refs', 'supporting_refs', 'counterevidence_refs')
                                              for r in row.get(name, [])})
            compact['text_truncated'] = any(isinstance(v, str) and len(v) > 600 for k, v in row.items() if k in keep)
            groups[field].append(compact)
    coverage = {k: v for k, v in local['coverage'].items() if k not in {'limitations', 'pending_segments', 'unselected_segments'}}
    coverage.update(pending_count=len(local['coverage'].get('pending_segments', [])),
                    unselected_count=len(local['coverage'].get('unselected_segments', [])))
    payload = {**groups, 'coverage': coverage,
               'input_coverage': {field: {'available': len(rows), 'included': len(rows)} for field, rows in groups.items()},
               'evidence_scope': 'Summarize supplied validated judgments only. Summary input may be sampled; all local results remain saved. '
                                 'source_lines are navigation; quotes are bounded excerpts, not complete focus coverage.'}
    # Reserve space for exact quotes and their pointer metadata. Never remove saved checkpoint results.
    limit = max(1000, int(max_prompt_chars * .65))
    while len(json.dumps(payload, ensure_ascii=False)) > limit and any(groups.values()):
        field = max(fields, key=lambda f: len(json.dumps(groups[f], ensure_ascii=False)))
        groups[field].pop()
        payload['input_coverage'][field]['included'] = len(groups[field])
    refs = []
    for field, rows in groups.items():
        for row in local.get(field, [])[:len(rows)]:
            if row.get('status') == 'unknown' or row.get('epistemic_status') == 'unknown':
                continue
            refs += row.get('evidence_refs', row.get('supporting_refs', []))[:1]
    refs = list({json.dumps(r, sort_keys=True): r for r in refs}.values())[:20]
    quote_limit = min(6000, max_prompt_chars // 5)
    per_quote = min(600, max(1, quote_limit // max(1, len(refs))))
    packets = [{'returned_ref': r, 'returned_chars': per_quote} for r in refs]
    return payload, packets


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
