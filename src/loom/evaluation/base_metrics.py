"""Shared, deterministic execution accounting. No model calls or workspace reads."""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from math import ceil, isfinite

from loom.core import thaw_json
from loom.trace_analysis.links import tool_output

VERSION = "loom.base-facts.v1"
STATUSES = ("success", "failed", "cancelled", "incomplete", "unknown")
FAMILIES = {
    "read_file": "file_read", "list_directory": "file_read", "write_file": "file_write", "edit_file": "file_write",
    "shell_execute": "command", "process_execute": "command", "search_files": "search", "web_search": "search",
    "fetch_url": "network", "knowledge_search": "knowledge", "create_report": "document", "docs_output": "document",
    "finish": "control", "enter_plan": "control", "submit_plan": "control", "update_plan": "control",
}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return round(parsed.timestamp() * 1000) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def terminal_event(item, events=(), kind="tool"):
    terminal = item.failed_event or item.completed_event
    if terminal:
        return terminal
    start = item.started_event if kind == "tool" else item.requested_event
    identifier = item.tool_call_id if kind == "tool" else item.llm_call_id
    return next((e for e in events if e.event_type in {f"{kind}.cancelled", f"{kind}.interrupted"}
                 and (e.run_id, e.loop_id, e.trace_id, e.step_number) == (item.run_id, item.loop_id, item.trace_id, item.step_number)
                 and (e.tool_call_id if kind == "tool" else e.llm_call_id) == identifier
                 and (start is None or (e.line_number or 0) >= (start.line_number or 0))), None)


def tool_result(item, events=()):
    terminal = terminal_event(item, events)
    output, _ = tool_output(terminal) if terminal else (None, None)
    output = output if isinstance(output, Mapping) else {}
    # Some producers record a Result wrapping an Observation.
    for _ in range(3):
        if isinstance(output.get("value"), Mapping) and output.get("ok") is not False:
            output = output["value"]
        else:
            break
    error = (terminal.payload.get("error") or output.get("error") or {}) if terminal else {}
    code = str(error.get("code", "")) if isinstance(error, Mapping) else ""
    if output.get("cancelled") is True or "CANCEL" in code or (terminal and terminal.event_type == "tool.cancelled"):
        return "cancelled", "cancelled", output
    if output.get("timed_out") is True or "TIMEOUT" in code or "TIMED_OUT" in code:
        return "failed", "timeout", output
    if "UNKNOWN" in code or "UNCERTAIN" in code or (terminal and terminal.event_type == "tool.interrupted"):
        return "unknown", "uncertain_effect", output
    failed = item.failed_event is not None or output.get("ok") is False or output.get("accepted") is False
    failed |= output.get("exit_code") not in (None, 0) and output.get("status") != "no_match"
    if failed:
        category = ("rejected" if output.get("accepted") is False else "invalid_input" if "VALIDATION" in code else "permission" if "PERMISSION" in code else
                    "network" if any(s in code for s in ("NETWORK", "CONNECTION", "HTTP")) else "execution")
        return "failed", category, output
    return ("success" if item.completed_event else "incomplete"), None, output


def model_status(item, events=()):
    end = terminal_event(item, events, "llm")
    error = end.payload.get("error", {}) if end else {}
    code = str(error.get("code", "")) if isinstance(error, Mapping) else ""
    if "CANCEL" in code or (end and end.event_type == "llm.cancelled"):
        return "cancelled"
    if end and end.event_type == "llm.interrupted":
        return "unknown"
    return "failed" if item.failed_event else "success" if item.completed_event else "incomplete"


def duration(start, end):
    recorded = end.payload.get("duration_ms") if end else None
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool) and isfinite(recorded) and recorded >= 0:
        return recorded, "recorded"
    a, b = timestamp(start.at) if start else None, timestamp(end.at) if end else None
    return (b - a, "timestamps") if a is not None and b is not None and b >= a else (None, "missing_or_invalid")


def distribution(values):
    known = sorted(v for v in values if v is not None)
    return {"measured": len(known), "missing": len(values) - len(known), "total_ms": sum(known) if known else None,
            "average_ms": sum(known) / len(known) if known else None,
            "p95_ms": known[ceil(len(known) * .95) - 1] if known else None, "max_ms": max(known) if known else None}


def aggregate(rows):
    counts = Counter(r["status"] for r in rows)
    resolved = counts["success"] + counts["failed"]
    result = {"calls": len(rows), **{k: counts[k] for k in STATUSES},
              "success_rate": counts["success"] / resolved if resolved else None,
              "duration": distribution([r["duration_ms"] for r in rows]),
              "failure_reasons": dict(Counter(r["failure_reason"] for r in rows if r.get("failure_reason"))),
              "truncated_outputs": sum(r.get("output_truncated") is True for r in rows),
              "explicit_retries": sum(bool(r.get("retry_of")) for r in rows)}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [r.get("usage", {}).get(field) for r in rows]
        known = [v for v in values if v is not None]
        result[field] = {"known": sum(known), "measured_calls": len(known), "missing_calls": len(rows) - len(known),
                         "average": sum(known) / len(known) if known else None, "max": max(known) if known else None}
    signatures = Counter(r["input_signature"] for r in rows if r.get("input_signature"))
    result["repeated_inputs"] = sum(n - 1 for n in signatures.values() if n > 1)
    return result


def grouped(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row.get(key) or "unknown"].append(row)
    return [{"name": name, **aggregate(values)} for name, values in sorted(groups.items())]


def call_ledger(store, graph, tokens):
    ledger = {r["round_id"]: r for r in tokens}
    models, tools = [], []
    for kind, items, target in (("model", graph.llm_rounds, models), ("tool", graph.tool_calls, tools)):
        for item in items:
            start = item.requested_event if kind == "model" else item.started_event
            end = terminal_event(item, graph.events, "llm" if kind == "model" else "tool")
            anchor = start or end
            if anchor is None:
                continue
            p = anchor.payload
            meta = p.get("metadata") if isinstance(p.get("metadata"), Mapping) else {}
            role = p.get("usage_role") or meta.get("usage_role") or "solver"
            elapsed, basis = duration(start, end)
            row = {"id": item.id, "run_id": item.run_id, "role": role,
                   "has_start": start is not None, "has_terminal": end is not None,
                   "stage": p.get("acceptance_stage") or meta.get("acceptance_stage") or role,
                   "started_at": start.at if start else None, "ended_at": end.at if end else None,
                   "duration_ms": elapsed, "duration_basis": basis,
                   "retry_of": p.get("retry_of") or meta.get("retry_of"),
                   "evidence_refs": [asdict(store.ref(e)) for e in (start, end) if e],
                   "seq": p.get("session_seq", anchor.line_number)}
            if kind == "model":
                row.update(model=p.get("model") or "unknown", status=model_status(item, graph.events),
                           llm_call_id=item.llm_call_id, usage=ledger.get(item.id, {}))
                error = end.payload.get("error", {}) if end else {}
                code = str(error.get("code", "")) if isinstance(error, Mapping) else ""
                if row["status"] == "failed":
                    row["failure_reason"] = "timeout" if "TIMEOUT" in code or "TIMED_OUT" in code else "provider"
            else:
                status, reason, output = tool_result(item, graph.events)
                row.update(tool=item.tool_id, tool_call_id=item.tool_call_id, family=FAMILIES.get(item.tool_id, "other"),
                           status=status, failure_reason=reason, output_truncated=output.get("truncated"), exit_code=output.get("exit_code"))
                if start and "input" in start.payload:
                    row["input_signature"] = hashlib.sha256(json.dumps(
                        [item.run_id, item.tool_id, thaw_json(start.payload["input"])], sort_keys=True).encode()).hexdigest()
            target.append(row)
    return {"models": models, "tools": tools}


def runtime_cost(events, calls):
    """Partition lifecycle intervals, then sweep calls within each recorded running interval."""
    by_run = defaultdict(list)
    for e in events:
        if e.run_id and e.event_type.startswith(("run.", "step.", "llm.", "tool.", "acceptance.", "verification.")):
            at = timestamp(e.at)
            if at is not None:
                by_run[e.run_id].append((at, e))
    transitions = {"run.started": "running", "run.completed": "terminal", "run.failed": "terminal", "run.stopped": "terminal",
                   "run.paused": "paused", "run.suspended": "paused", "run.recovery.required": "paused"}
    states = {"running": "running", "queued": "queued", "awaiting_input": "waiting_input", "paused": "paused", "suspended": "paused",
              "completed": "terminal", "failed": "terminal", "stopped": "terminal"}
    totals = Counter()
    activity = Counter()
    intervals, run_rows = [], []
    reversed_timestamps = 0
    for run, entries in by_run.items():
        # Sequence is authoritative; clock reversals remain visible as a data-quality issue.
        reversed_timestamps += sum(b[0] < a[0] for a, b in zip(entries, entries[1:], strict=False))
        start, end = entries[0][0], max(at for at, _ in entries)
        state, cursor = "unknown", start
        spans = []
        for at, event in entries:
            new = states.get(event.payload.get("state"), "unknown") if event.event_type == "run.state.changed" else transitions.get(event.event_type)
            if new is None:
                continue
            at = max(cursor, at)
            if at > cursor:
                spans.append((cursor, at, state))
            cursor, state = at, new
        if end > cursor:
            spans.append((cursor, end, state))
        # Terminal-to-next-start gaps are not active run time.
        run_totals = Counter()
        for a, b, state in spans:
            name = "inactive" if state == "terminal" else state
            totals[name] += b - a
            run_totals[name] += b - a
            intervals.append({"run_id": run, "start_ms": a, "end_ms": b, "state": name})
            if state != "running":
                continue
            changes = defaultdict(Counter)
            changes[a]
            changes[b]
            for kind, rows in calls.items():
                for call in rows:
                    if call["run_id"] != run:
                        continue
                    left, right = timestamp(call["started_at"]), timestamp(call["ended_at"])
                    if left is None or right is None or right < left:
                        continue
                    left, right = max(a, left), min(b, right)
                    if right <= left:
                        continue
                    changes[left][kind] += 1
                    changes[right][kind] -= 1
            active, last = Counter(), a
            for at in sorted(changes):
                label = ("overlap" if active["models"] and active["tools"] else "model_only" if active["models"] else
                         "tool_only" if active["tools"] else "unattributed")
                activity[label] += at - last
                active.update(changes[at])
                last = at
        run_rows.append({"run_id": run, "wall_ms": end - start, "states_ms": dict(run_totals)})
    timestamps = [at for values in by_run.values() for at, _ in values]
    return {"basis": "recorded_lifecycle_and_timestamp_intervals", "wall_ms": sum(r["wall_ms"] for r in run_rows) if run_rows else None,
            "session_span_ms": max(timestamps) - min(timestamps) if timestamps else None,
            "states_ms": {s: totals[s] for s in ("running", "queued", "waiting_input", "paused", "inactive", "unknown")},
            "running_activity_ms": {s: activity[s] for s in ("model_only", "tool_only", "overlap", "unattributed")},
            "runs": run_rows, "intervals": intervals, "clock_reversals": reversed_timestamps,
            "missing_timestamp_events": sum(timestamp(e.at) is None for e in events),
            "limitations": ["Call durations are cumulative and can overlap. Acceptance is a purpose, not an additional time slice.",
                            "Unattributed time is not measured framework overhead. Provider-internal retries and queue time require explicit instrumentation."]}


def build_base_statistics(store, graph, token_rows, *, coverage=None, acceptance=None, run_id=None, _calls=None):
    coverage = coverage or {}
    calls = _calls or call_ledger(store, graph, token_rows)
    events = [e for e in store.events if run_id is None or e.run_id == run_id]
    if run_id is not None:
        calls = {kind: [row for row in rows if row["run_id"] == run_id] for kind, rows in calls.items()}
        token_rows = [r for r in token_rows if r["run_id"] == run_id]
        latest = next((e for e in reversed(events) if e.event_type.startswith("acceptance.") and e.payload.get("acceptance")), None)
        acceptance = {"state": thaw_json(latest.payload["acceptance"]), "evidence_refs": [asdict(store.ref(latest))]} if latest else None
    timing = runtime_cost(events, calls)
    types = Counter(e.event_type for e in events)
    # A stable trace/step can continue under a new loop after resume.
    steps = defaultdict(list)
    for step in graph.steps:
        if run_id is not None and step.run_id != run_id:
            continue
        steps[(step.run_id, step.trace_id, step.step_number)].append(step)
    failed_steps = {(e.run_id, e.trace_id, e.step_number) for e in events if e.event_type == "step.failed" or
                    (e.event_type == "trace.completed" and e.payload.get("outcome") in {"fail", "failed"})}
    step_states = Counter("completed" if any(s.completed_event for s in group) else "failed" if key in failed_steps else "incomplete"
                          for key, group in steps.items())
    artifacts, files = {}, set()
    for e in events:
        if e.event_type == "artifact.created":
            ref = e.payload.get("artifact", {})
            if isinstance(ref, Mapping) and ref.get("sha256"):
                artifacts[ref["sha256"]] = thaw_json(ref)
        if e.event_type == "tool.completed" and e.tool_id in {"write_file", "edit_file", "create_report"}:
            output, _ = tool_output(e)
            if isinstance(output, Mapping) and output.get("ok") is not False:
                path = output.get("path") or output.get("workspace_path")
                if path:
                    files.add(str(path))
    result = {"schema_version": VERSION, "scope": "run" if run_id else "source_snapshot", "source_sha256": store.source_sha256,
            "basic": {"events": {"raw": coverage.get("raw_event_count", len(events)), "analyzed": len(events),
                                 "by_type": coverage.get("event_type_counts", dict(types))},
                      "runs": len({e.run_id for e in events if e.run_id}), "steps": len(steps), "step_states": dict(step_states),
                      "step_attempts": types["step.started"], "run_attempts": types["run.started"],
                      "goal_changes": types["task.goal.revised"], "acceptance": acceptance},
            "tools": {**aggregate(calls["tools"]), "by_tool": grouped(calls["tools"], "tool"),
                      "by_family": grouped(calls["tools"], "family"), "by_role": grouped(calls["tools"], "role")},
            "models": {**aggregate(calls["models"]), "by_model": grouped(calls["models"], "model"),
                       "by_role": grouped(calls["models"], "role"), "by_stage": grouped(calls["models"], "stage")},
            "timing": timing, "calls": calls,
            "outputs": {"artifacts": list(artifacts.values()), "workspace_files": sorted(files)},
            "quality": {"missing_artifacts": coverage.get("missing_artifacts", []),
                        "missing_usage_calls": sum(r.get("total_tokens") is None for r in token_rows),
                        "usage_statuses": dict(Counter(r["usage_status"] for r in token_rows)),
                        "unscoped_call_events": len({e.line_number for e in graph.orphaned_events if e.event_type in {
                            "llm.requested", "llm.completed", "llm.failed", "tool.started", "tool.completed", "tool.failed"}
                            and (run_id is None or e.run_id == run_id)}),
                        "calls_without_start": sum(not r["has_start"] for rows in calls.values() for r in rows),
                        "calls_without_terminal": sum(not r["has_terminal"] for rows in calls.values() for r in rows),
                        "duplicate_records": sum(e.line_number in store.coverage.get("repeated_record_lines", []) for e in events),
                        "missing_call_durations": sum(r["duration_ms"] is None for rows in calls.values() for r in rows),
                        "success_rate_basis": "success / (success + failed); cancelled, incomplete and unknown excluded",
                        "token_basis": "provider_reported; missing usage is not zero; semantic efficiency is not assessed",
                        "retry_basis": "explicit retry_of only; repeated calls are not automatically retries",
                        "unavailable": ["provider cache/reasoning usage unless recorded", "provider-internal attempts",
                                        "time to first token", "monetary cost"]}}
    lifecycle = {"run.started": "running", "run.completed": "completed", "run.failed": "failed", "run.stopped": "stopped",
                 "run.paused": "paused", "run.suspended": "suspended"}
    result["execution_status"] = next((e.payload.get("state") if e.event_type == "run.state.changed" else lifecycle[e.event_type]
                                      for e in reversed(events) if e.event_type == "run.state.changed" or e.event_type in lifecycle), "not_started")
    result["basic"]["interruptions"] = {
        "pauses": sum(e.event_type in {"run.paused", "run.suspended", "run.recovery.required"} or
                      (e.event_type == "run.state.changed" and e.payload.get("state") in {"paused", "suspended"}) for e in events),
        "input_waits": sum(e.event_type == "run.state.changed" and e.payload.get("state") == "awaiting_input" for e in events),
        "uncertain_operations": types["operation.uncertain"],
    }
    if run_id is None:
        result["by_run"] = {}
        for rid in dict.fromkeys(e.run_id for e in events if e.run_id):
            counts = coverage.get("event_counts_by_run", {}).get(rid)
            run_coverage = {"missing_artifacts": [r for r in coverage.get("missing_artifacts", [])
                                                 if any(e.run_id == rid and e.payload.get("session_seq") == r.get("seq") for e in events)]}
            if counts:
                run_coverage.update(raw_event_count=sum(counts.values()), event_type_counts=counts)
            result["by_run"][rid] = build_base_statistics(store, graph, token_rows, coverage=run_coverage, run_id=rid, _calls=calls)
    return result
