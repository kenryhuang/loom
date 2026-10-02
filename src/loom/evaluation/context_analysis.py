"""Exact observed context changes; repetition is not a waste diagnosis."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict
from typing import Any

from loom.core import thaw_json
from loom.evaluation.evidence_store import EvidenceStore, text_value
from loom.trace_analysis.schemas import EpisodeGraph, NormalizedEvent


def request_fields(event: NormalizedEvent) -> tuple[Any, str, Any, str]:
    nested = event.payload.get("request")
    payload, prefix = (nested, "request.") if isinstance(nested, Mapping) else (event.payload, "")
    return payload.get("messages"), prefix + "messages", payload.get("tools"), prefix + "tools"


def _units(store: EvidenceStore, event: NormalizedEvent, values: Any, path: str, kind: str) -> list[dict]:
    if not isinstance(values, list | tuple):
        return []
    units = []
    for index, value in enumerate(values):
        canonical = json.dumps(thaw_json(value), sort_keys=True, ensure_ascii=False)
        content = value.get("content", value) if isinstance(value, Mapping) else value
        text = text_value(content)
        units.append({"id": hashlib.sha256(canonical.encode()).hexdigest(), "kind": kind, "index": index,
                      "role": value.get("role") if isinstance(value, Mapping) else None,
                      "ref": asdict(store.ref(event, f"{path}.{index}")), "excerpt": text[:320],
                      "char_length": len(text), "excerpt_truncated": len(text) > 320})
    return units


def _delta(previous: list[dict], current: list[dict]) -> dict:
    remaining = Counter(u["id"] for u in previous)
    added, retained = [], []
    for unit in current:
        if remaining[unit["id"]]:
            retained.append(unit)
            remaining[unit["id"]] -= 1
        else:
            added.append(unit)
    removed = []
    for unit in previous:
        if remaining[unit["id"]]:
            removed.append(unit)
            remaining[unit["id"]] -= 1
    return {"added": added, "retained": retained, "removed": removed}


def build_context_deltas(store: EvidenceStore, graph: EpisodeGraph) -> tuple[dict, ...]:
    previous: dict[tuple, tuple] = {}
    rows = []
    for round_item in graph.llm_rounds:
        key = round_item.run_id, round_item.loop_id
        prior_id, prior_messages, prior_tools = previous.get(key, (None, [], []))
        event = round_item.requested_event
        messages, message_path, tools, tool_path = request_fields(event) if event else (None, "", None, "")
        units = _units(store, event, messages, message_path, "message") if event else []
        schemas = _units(store, event, tools, tool_path, "tool_schema") if event else []
        observed = isinstance(messages, list | tuple)
        row = {"round_id": round_item.id, "run_id": round_item.run_id, "loop_id": round_item.loop_id,
               "previous_round_id": prior_id, "context_status": "recorded" if observed else "unknown",
               "message_count": len(units) if observed else None,
               "content_char_length": sum(u["char_length"] for u in units) if observed else None,
               "repeated_unit_count": len(units) - len({u["id"] for u in units}),
               "waste_status": "not_assessed", "units": units,
               **(_delta(prior_messages, units) if observed else {"added": [], "retained": [], "removed": []}),
               "tool_schemas": {"status": "recorded" if isinstance(tools, list | tuple) else "unknown",
                                "units": schemas, **(_delta(prior_tools, schemas) if isinstance(tools, list | tuple)
                                                     else {"added": [], "retained": [], "removed": []})},
               "evidence_refs": [asdict(store.ref(event))] if event else [],
               "interpretation_limits": "Exact recorded content identity does not establish relevance or semantic consumption."}
        rows.append(row)
        if observed:
            previous[key] = round_item.id, units, schemas if isinstance(tools, list | tuple) else prior_tools
    return tuple(rows)


def observed_injections(store: EvidenceStore, graph: EpisodeGraph, tool, output: Any) -> list[dict]:
    """Find later messages carrying a result, never infer use from the final reply."""
    output_event = tool.completed_event or tool.failed_event
    if output_event is None:
        return []
    rows = []
    raw = thaw_json(output)
    output_text = text_value(raw)
    for round_item in graph.llm_rounds:
        request = round_item.requested_event
        if request is None or request.line_number <= output_event.line_number:
            continue
        if (round_item.run_id, round_item.loop_id) != (tool.run_id, tool.loop_id):
            continue
        messages, path, _, _ = request_fields(request)
        if not isinstance(messages, list | tuple):
            continue
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            text = text_value(content)
            native = tool.tool_call_id is not None and message.get("tool_call_id") == tool.tool_call_id
            decoded = None
            if isinstance(content, str):
                with suppress(ValueError):
                    decoded = json.loads(content)
            exact = raw is not None and (content == output or decoded == raw or text == output_text)
            basis = "tool_call_id" if native else "exact_output" if exact else None
            # Legacy injected messages often wrap output as JSON with a tool label.
            omitted = []
            if (basis is None and isinstance(decoded, dict) and raw is not None
                    and any(decoded.get(k) == raw for k in ("output", "result", "value"))):
                basis = "wrapped_exact_output"
                exact = True
            if basis is None and isinstance(content, str) and content.startswith("Tool execution transcript:\n"):
                entries = None
                with suppress(ValueError):
                    entries = json.loads(content.split("\n", 1)[1])
                for entry in entries if isinstance(entries, list) else []:
                    if not isinstance(entry, dict) or entry.get("tool") != tool.tool_id:
                        continue
                    recorded_input = (tool.started_event or output_event).payload.get("input")
                    if "input" in entry and entry["input"] != thaw_json(recorded_input):
                        continue
                    injected = entry.get("result")
                    exact = injected == raw
                    subset = (isinstance(injected, dict) and bool(injected) and isinstance(raw, dict)
                              and all(k in raw and raw[k] == v for k, v in injected.items()))
                    if exact or subset:
                        basis = "recorded_tool_transcript"
                        omitted = sorted(set(raw) - set(injected)) if subset else []
                        break
            if basis is not None:
                rows.append({"round_id": round_item.id, "ref": asdict(store.ref(request, f"{path}.{index}")),
                             "basis": basis, "raw_output_preserved": exact, "char_length": len(text),
                             "omitted_output_fields": omitted,
                             "source_identity": "tool_call_id" if native else "content_match_only",
                             "semantic_consumption": "unknown"})
    return rows
