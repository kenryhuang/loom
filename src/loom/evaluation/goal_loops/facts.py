"""Run views preserve original EvidenceStore pointers and provider accounting."""

import hashlib
import json
from dataclasses import asdict

from loom.core import thaw_json
from loom.evaluation.evidence_store import text_value


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def run_facts(source, facts, run_id):
    events = [e for e in source.events if e.run_id == run_id]
    if not isinstance(run_id, str) or not events:
        raise ValueError("Select a run recorded in this snapshot")
    calls = facts["base"]["statistics"]["calls"]
    atoms = []
    for kind, rows in calls.items():
        for row in rows:
            if row["run_id"] != run_id:
                continue
            usage = row.get("usage", {})
            atoms.append(
                {
                    **row,
                    "id": row["id"],
                    "kind": "model" if kind == "models" else "tool",
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "usage_status": usage.get("usage_status"),
                }
            )
    atom_ids = {r["id"] for r in atoms}
    tools = {t["id"]: t for t in facts["tool_uses"] if t["id"] in atom_ids}
    models = {c["id"]: c for c in facts["model_calls"] if c["id"] in atom_ids}
    # Public response content only: do not deliver provider hidden reasoning fields or entire repeated prompts.
    event_fields = {}
    for row in models.values():
        for key in ("request_ref", "response_ref"):
            ref = row.get(key)
            if not ref:
                continue
            event = source.event(ref)
            if key == "response_ref":
                value = source.resolve(ref)
                path = ref.get("field_path")
                if isinstance(value, dict) and isinstance(value.get("content"), str):
                    path = (path + "." if path else "") + "content"
                    event_fields.setdefault(event.line_number, []).append(asdict(source.ref(event, path)))
                if isinstance(value, dict) and value.get("tool_calls"):
                    path = (ref.get("field_path") + "." if ref.get("field_path") else "") + "tool_calls"
                    event_fields.setdefault(event.line_number, []).append(asdict(source.ref(event, path)))
            else:
                event_fields.setdefault(event.line_number, [])  # Indexed request, costs/identity supplied separately.
    for row in tools.values():
        for key in ("input_ref", "raw_output_ref"):
            ref = row.get(key)
            if ref:
                event_fields.setdefault(ref["line_number"], []).append(ref)
    calls_by_line = {}
    for atom in atoms:
        for ref in atom["evidence_refs"]:
            calls_by_line.setdefault(ref["line_number"], []).append(atom["id"])
    records = []
    for e in events:
        refs = event_fields.get(e.line_number)
        if refs is None:
            refs = [
                asdict(source.ref(e, k))
                for k in (
                    "objective",
                    "reason",
                    "message",
                    "state",
                    "acceptance",
                    "result",
                    "plan",
                    "workflow",
                    "artifact",
                    "description",
                    "error",
                    "output",
                    "criteria",
                    "checks",
                    "summary",
                    "definition",
                    "outcome",
                    "subject",
                    "status",
                    "report",
                )
                if k in e.payload
            ]
        if e.event_type == "message.created" and "content" in e.payload:
            refs.append(asdict(source.ref(e, "content")))
        records.append(
            dict(
                source_ref=asdict(source.ref(e)),
                line=e.line_number,
                seq=e.payload.get("session_seq"),
                event_id=e.record_id,
                type=e.event_type,
                at=e.at,
                refs=refs,
                call_ids=calls_by_line.get(e.line_number, []),
            )
        )
    episodes = {e["id"] for e in facts["episodes"] if e.get("run_id") == run_id}
    goals = [g for g in facts["goal_revisions"] if g["episode_id"] in episodes and g.get("applied_line") is not None and not g.get("covered_by_revision")]
    roots = []
    record_map = {r["line"]: r for r in records}
    for g in goals:
        origin_refs = list(g.get("evidence_refs", []))
        if g.get("visibility_ref"):
            origin_refs.append(g["visibility_ref"])
        precise_refs = []
        for ref in origin_refs:
            event = source.event(ref)
            if ref.get("field_path"):
                precise_refs.append(ref)
            else:
                for field in ("objective", "content", "task.objective", "metadata.objective"):
                    try:
                        pointer = asdict(source.ref(event, field))
                        if source.resolve(pointer) == g["objective"]:
                            precise_refs.append(pointer)
                    except ValueError:
                        continue
        goal_refs = precise_refs or origin_refs
        for ref in precise_refs:
            if ref["line_number"] in record_map and ref not in record_map[ref["line_number"]]["refs"]:
                record_map[ref["line_number"]]["refs"].append(ref)
        roots.append(
            dict(
                id="goal:" + identity(source.source_sha256, run_id, g["id"]),
                parent_goal_id=None,
                objective=g["objective"],
                conditions=g.get("criteria", []),
                evidence_refs=goal_refs,
                source_goal_id=g["id"],
                origin_line=g["applied_line"],
                basis="recorded",
                confidence=None,
                goal_disposition="unknown",
                completion_claim="unknown",
            )
        )
    if not roots:
        roots = [
            dict(
                id="goal:" + identity(source.source_sha256, run_id),
                parent_goal_id=None,
                objective="Goal not recorded",
                conditions=[],
                evidence_refs=[],
                origin_line=events[0].line_number,
                basis="unknown",
                confidence=None,
                goal_disposition="unknown",
                completion_claim="unknown",
            )
        ]
    roots.sort(key=lambda g: g["origin_line"])
    for i, root in enumerate(roots):
        root["end_line"] = roots[i + 1]["origin_line"] - 1 if i + 1 < len(roots) else events[-1].line_number
        root["supersedes"] = roots[i - 1]["id"] if i else None
    stats = facts["base"]["statistics"].get("by_run", {}).get(run_id, {})
    return dict(
        run_id=run_id,
        source_sha256=source.source_sha256,
        scope_digest=identity(source.source_sha256, run_id, [e.line_number for e in events]),
        roots=roots,
        atoms=atoms,
        records=records,
        statistics=stats,
        verification=[
            dict(line=e.line_number, type=e.event_type, value=thaw_json(e.payload), evidence_refs=[asdict(source.ref(e))])
            for e in events
            if e.event_type.startswith(("verification.", "acceptance.gate."))
        ],
        limitations=[
            "Unrecorded provider transport retries cannot be reconstructed",
            "Snapshot omits most stream deltas; source lines differ from session sequence numbers",
        ],
    )


def packets(source, view, page_chars=6000):
    """Full eligible fields are paged, never silently reduced to head/tail summaries."""
    result = []
    for record in view["records"]:
        if not record["refs"]:
            result.append(dict(id="e:" + str(record["line"]), **record, content="", evidence_ref=None, full_field=True))
        for ref in record["refs"]:
            value = text_value(source.resolve(ref))
            for start in range(0, max(1, len(value)), page_chars):
                offset = ref.get("start") or 0
                precise = {**ref, "start": offset + start, "end": offset + min(len(value), start + page_chars)}
                result.append(
                    dict(
                        id="p:" + identity(precise),
                        line=record["line"],
                        seq=record["seq"],
                        type=record["type"],
                        call_ids=record["call_ids"],
                        content=value[start : start + page_chars],
                        evidence_ref=precise,
                        full_field=len(value) <= page_chars,
                    )
                )
    return result
