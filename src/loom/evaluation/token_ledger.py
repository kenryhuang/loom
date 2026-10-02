"""Provider-reported per-call accounting with explicit uncertainty."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from loom.evaluation.evidence_store import EvidenceStore
from loom.trace_analysis.schemas import EpisodeGraph, NormalizedEvent

_FIELDS = {"prompt_tokens": ("prompt_tokens", "promptTokens", "input_tokens"),
           "completion_tokens": ("completion_tokens", "completionTokens", "output_tokens"),
           "total_tokens": ("total_tokens", "totalTokens", "total")}


def _key(event: NormalizedEvent) -> tuple:
    return event.run_id, event.loop_id, event.trace_id, event.step_number, event.llm_call_id


def _usage(event: NormalizedEvent) -> tuple[Any, str | None]:
    response = event.payload.get("response")
    if isinstance(response, Mapping) and "usage" in response:
        return response["usage"], "response.usage"
    if "usage" in event.payload:
        return event.payload["usage"], "usage"
    return None, None


def _parse(value: Any) -> tuple[dict, bool]:
    result, conflict = {}, False
    for name, aliases in _FIELDS.items():
        reported = [value[k] for k in aliases if isinstance(value, Mapping) and k in value]
        valid = [v for v in reported if isinstance(v, int) and not isinstance(v, bool) and v >= 0]
        inconsistent = len(set(valid)) > 1
        conflict |= inconsistent
        result[name] = valid[0] if valid and len(valid) == len(reported) and not inconsistent else None
    if all(v is not None for v in result.values()):
        conflict |= result["prompt_tokens"] + result["completion_tokens"] != result["total_tokens"]
    return result, conflict


def build_token_ledger(store: EvidenceStore, graph: EpisodeGraph) -> tuple[tuple[dict, ...], dict]:
    rows, registered = [], set()
    completions: dict[tuple, list] = {}
    for event in store.events:
        if event.event_type == "llm.completed":
            completions.setdefault(_key(event), []).append(event)
    for round_item in graph.llm_rounds:
        anchor = round_item.requested_event or round_item.completed_event or round_item.failed_event
        key = _key(anchor) if anchor else None
        events = completions.get(key, [])
        siblings = [r for r in graph.llm_rounds
                    if (r.run_id, r.loop_id, r.trace_id, r.step_number, r.llm_call_id) == key]
        if len(siblings) > 1:
            if round_item.completed_event is None:
                events = []
            else:
                start = anchor.line_number
                later_starts = [(r.requested_event or r.completed_event or r.failed_event).line_number for r in siblings
                                if (r.requested_event or r.completed_event or r.failed_event).line_number > start]
                end = min(later_starts) if later_starts else float("inf")
                events = [event for event in events if start <= event.line_number < end]
        registered.update(e.line_number for e in events)
        samples, refs = [], []
        for event in events:
            usage, path = _usage(event)
            parsed, conflict = _parse(usage)
            samples.append((parsed, conflict))
            refs.append(asdict(store.ref(event, path)))
        fields = {name: None for name in _FIELDS}
        status = "unknown"
        if samples:
            conflict = any(c for _, c in samples) or any(v != samples[0][0] for v, _ in samples[1:])
            if conflict:
                status = "conflicting"
            else:
                fields = samples[0][0]
                status = "complete" if all(v is not None for v in fields.values()) else "partial" if any(v is not None for v in fields.values()) else "unknown"
                if len(samples) > 1 and status == "complete":
                    status = "duplicate"
        rows.append({"round_id": round_item.id, "run_id": round_item.run_id,
                     "llm_call_id": round_item.llm_call_id, **fields, "usage_status": status,
                     "usage_record_count": len(events), "evidence_refs": refs,
                     "accounting_basis": "provider_reported", "character_token_estimate": None})
    auxiliary = []
    orphan_lines = {event.line_number for event in graph.orphaned_events}
    for event in store.events:
        if event.event_type.startswith("llm.") and event.line_number not in registered:
            usage, path = _usage(event)
            if path is not None or event.line_number in orphan_lines:
                auxiliary.append({"llm_call_id": event.llm_call_id, "run_id": event.run_id,
                                  "event_type": event.event_type, "usage": store.resolve(store.ref(event, path)) if path else None,
                                  "ref": asdict(store.ref(event, path)), "accounting_status": "unattributed"})
    complete = bool(rows) and all(r["usage_status"] == "complete" for r in rows) and not auxiliary
    coverage = {"status": "complete" if complete else "incomplete", "call_count": len(rows),
                "auxiliary_usage": auxiliary, "unmeasured_costs": ["cache usage when not reported", "source-level token attribution"],
                "character_estimates": "not_computed"}
    for name in _FIELDS:
        known = sum(r[name] for r in rows if r[name] is not None)
        coverage[name] = known if complete else None
        coverage[f"known_{name}"] = known
    return tuple(rows), coverage
