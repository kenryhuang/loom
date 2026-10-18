"""Bounded navigation over the complete persisted entity catalogue."""

import json


def brief(row):
    fields = (
        "id",
        "parent_goal_id",
        "goal_id",
        "attempt_id",
        "objective",
        "approach",
        "outcome",
        "remaining_questions",
        "delta",
        "conditions",
        "kind",
        "question",
        "start_line",
        "end_line",
    )
    result = {k: row[k][:240] if isinstance(row[k], str) else row[k] for k in fields if k in row}
    if "conditions" in result:
        result["conditions"] = [str(c)[:160] for c in result["conditions"][:8]]
    result["projection"] = "navigation_excerpt_request_object_for_full_details"
    return result


def catalog(objects):
    rows = [brief(r) for r in objects.values() if not r.get("superseded_by")]
    return [rows[i : i + 12] for i in range(0, len(rows), 12)]


def window(ids, registry, max_chars):
    """Choose complete evidence pages, reporting the omitted IDs explicitly."""
    selected, size = [], 0
    for pid in ids:
        length = len(json.dumps(registry[pid], ensure_ascii=False))
        if size + length > max_chars:
            continue
        selected.append(pid)
        size += length
    return selected


def detail(row, registry):
    """Replace verbose pointer copies with exact packet IDs; never lose persisted refs."""
    refs = row.get("evidence_refs", [])
    return {
        **{k: v for k, v in row.items() if k not in {"evidence_refs", "amendments"}},
        "evidence_packet_ids": [p["id"] for p in registry.values() if p.get("evidence_ref") in refs],
        "origin_refs": [r for r in refs if not any(p.get("evidence_ref") == r for p in registry.values())],
    }
