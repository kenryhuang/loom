"""Small, explicitly selective indexes for original trace evidence expansion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from loom.evaluation.diagnostics import FactAnalysis
from loom.evaluation.evidence_store import EvidenceStore


def _pick(row: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    return {name: row[name] for name in names if name in row}


def _lean_refs(value: Any) -> Any:
    if isinstance(value, Mapping):
        if "source_sha256" in value and "line_number" in value:
            return {key: item for key, item in value.items() if item is not None or key == "event_hash"}
        return {key: _lean_refs(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_lean_refs(item) for item in value]
    return value


def _unit(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick(row, ("id", "kind", "index", "role", "ref", "char_length"))
    excerpt = row.get("excerpt")
    if isinstance(excerpt, str):
        result.update(excerpt=excerpt[:120], excerpt_truncated=bool(row.get("excerpt_truncated")) or len(excerpt) > 120)
    return result


def _context(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick(row, ("round_id", "run_id", "loop_id", "previous_round_id", "context_status", "message_count",
                         "content_char_length", "repeated_unit_count", "waste_status", "evidence_refs", "interpretation_limits"))
    result.update(units=[_unit(u) for u in row.get("units", ())],
                  added_ids=[u["id"] for u in row.get("added", ())],
                  retained_ids=[u["id"] for u in row.get("retained", ())],
                  retained_count=len(row.get("retained", ())),
                  removed=[_unit(u) for u in row.get("removed", ())])
    schemas = row.get("tool_schemas")
    if isinstance(schemas, Mapping):
        result["tool_schemas"] = {"status": schemas.get("status", "unknown"),
                                  "units": [_unit(u) for u in schemas.get("units", ())],
                                  "added_ids": [u["id"] for u in schemas.get("added", ())],
                                  "retained_ids": [u["id"] for u in schemas.get("retained", ())],
                                  "retained_count": len(schemas.get("retained", ())),
                                  "removed": [_unit(u) for u in schemas.get("removed", ())]}
    result["detail_selection"] = "Current and removed units have short excerpts; added/retained entries refer to current unit ids. Expand refs."
    return result


def _tool(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in row.items() if key not in {"injections", "output_excerpt"}}
    excerpt = row.get("output_excerpt")
    if isinstance(excerpt, str):
        result.update(output_excerpt=excerpt[:120],
                      output_excerpt_truncated=bool(row.get("output_excerpt_truncated")) or len(excerpt) > 120)
    injections = row.get("injections", ())
    selected = list(injections) if len(injections) <= 3 else [*injections[:2], injections[-1]]
    result.update(injection_count=len(injections), injections=selected, injections_omitted=len(injections) - len(selected),
                  injection_selection="All when at most three; otherwise first two and last recorded injections. Expand indexed request refs for content.")
    return result


def _index(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick(row, ("id", "kind", "round_id"))
    if "kind" not in row and "tool_id" in row:
        result["tool_id"] = row["tool_id"]
    refs = row.get("evidence_refs", ())
    ref = row.get("raw_output_ref") or (refs[0] if refs else row.get("input_ref") or row.get("content_ref"))
    if ref is not None:
        result["ref"] = ref
    if row.get("input_ref") is not None and row["input_ref"] != ref:
        result["input_ref"] = row["input_ref"]
    return result


def _verification(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _pick(row, ("id", "kind", "run_id", "loop_id", "round_id", "tool_id", "tool_call_id", "line_number",
                         "execution_status", "criterion_status", "evidence_refs", "content_ref", "content_char_length",
                         "truncated", "path", "cwd", "exit_code", "timed_out", "scope", "oracle_limitations",
                         "reported_outcome", "reported_status", "criterion_id", "write_status"))
    if "command" in row:
        command = row["command"]
        result["command"] = command[:240] if isinstance(command, str) else command
        result["command_truncated"] = isinstance(command, str) and len(command) > 240
    outputs = row.get("outputs", {})
    result["outputs"] = {name: {**_pick(value, ("ref", "char_length")), "excerpt": value.get("excerpt", "")[:120],
                                "excerpt_truncated": bool(value.get("excerpt_truncated")) or len(value.get("excerpt", "")) > 120}
                         for name, value in outputs.items() if isinstance(value, Mapping)}
    for key in ("warnings", "test_summary_observations", "assertions"):
        entries = row.get(key, ())
        result[key + "_count"] = len(entries)
        result[key] = [{**_pick(item, ("ref", "execution_status", "basis")), "text": item.get("text", "")[:120],
                        "text_truncated": len(item.get("text", "")) > 120} for item in entries[:3]]
        result[key + "_omitted"] = max(0, len(entries) - 3)
    freshness = row.get("freshness")
    if isinstance(freshness, Mapping):
        writes = freshness.get("later_recorded_writes", ())
        result["freshness"] = {"artifact_binding": freshness.get("artifact_binding", "unknown"),
                               "later_recorded_write_count": len(writes),
                               "later_recorded_write_ids": [write["id"] for write in writes],
                               "detail_selection": "Write details are in verification_index or local verification_evidence; freshness is not assumed."}
    result["detail_selection"] = "Short excerpts and first three observations per category; counts include omitted items. Expand original refs."
    return result


def batch_payload(store: EvidenceStore, facts: FactAnalysis, batch: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Select round detail, preserving global task definitions and access to omitted evidence."""
    round_ids = {row.get("id") for row in batch}

    def local(row):
        return row.get("round_id") in round_ids or (not batch and row.get("round_id") is None)

    local_tools = [row for row in facts.tool_uses if local(row)]
    tool_keys = {(row.get("run_id"), row.get("loop_id"), row.get("tool_call_id")) for row in local_tools if row.get("tool_call_id")}
    local_verification = [row for row in facts.verification_evidence if local(row) or
                          (row.get("tool_call_id") and (row.get("run_id"), row.get("loop_id"), row.get("tool_call_id")) in tool_keys)]
    verification_ids = {row.get("id") for row in local_verification}
    tool_ids = {row.get("id") for row in local_tools}
    verification_index = [_index(row) for row in facts.verification_evidence if row.get("id") not in verification_ids]
    verified_tool_keys = {(row.get("run_id"), row.get("loop_id"), row.get("tool_call_id"))
                          for row in facts.verification_evidence if row.get("tool_call_id")}
    omitted_tools = [row for row in facts.tool_uses if row.get("id") not in tool_ids]
    tool_index = [_index(row) for row in omitted_tools if not row.get("tool_call_id") or
                  (row.get("run_id"), row.get("loop_id"), row.get("tool_call_id")) not in verified_tool_keys]
    payload = {"stage": "round_analysis", "source_sha256": store.source_sha256,
            "task_contracts": [dict(row) for row in facts.task_contracts], "trajectory": list(batch),
            "context_deltas": [_context(row) for row in facts.context_deltas if local(row)],
            "tool_uses": [_tool(row) for row in local_tools], "tool_index": tool_index,
            "token_ledger": [dict(row) for row in facts.token_ledger if local(row)],
            "verification_evidence": [_verification(row) for row in local_verification],
            "verification_index": verification_index, "coverage": facts.coverage,
            "navigation": {"original_evidence_access": "read_evidence", "source_scope": "Registered source snapshot only",
                           "task_contract_selection": "All recorded task definitions, including revisions; source refs preserved",
                           "context_selection": "Selected rounds only; unit content is excerpted, retained units are listed by id",
                           "tool_details_omitted": len(omitted_tools), "verification_details_omitted": len(verification_index),
                           "tool_index_duplicates_omitted": len(omitted_tools) - len(tool_index),
                           "index_selection": ("All nonlocal verification observations; other tools, including unlinked tools, are indexed "
                                               "unless already accessible through verification refs. Indexes are not reviewed evidence."),
                           "source_event_count": len(store.events),
                           "evidence_coverage": "Navigation selection does not establish full source review or semantic coverage"}}
    return {key: value if key == "task_contracts" else _lean_refs(value) for key, value in payload.items()}
