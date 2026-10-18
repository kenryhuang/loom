"""Read-only time and public execution projection; never infer semantic ownership."""

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict

from loom.evaluation.base_metrics import timestamp
from loom.evaluation.goal_loops.costs import summarize

VERSION = "goal-execution-view.4"


def time_partition(calls):
    """Disjoint timestamp intervals. Nested tool/model calls must not be added twice."""
    changes = defaultdict(lambda: dict(model=0, tool=0))
    measured = 0
    for call in calls:
        a, b = timestamp(call.get("started_at")), timestamp(call.get("ended_at"))
        if a is None or b is None or b < a:
            continue
        measured += 1
        changes[a][call["kind"]] += 1
        changes[b][call["kind"]] -= 1
    parts = dict(model_only_ms=0, tool_only_ms=0, overlap_ms=0, gap_ms=0)
    active, previous = dict(model=0, tool=0), None
    for point, change in sorted(changes.items()):
        if previous is not None:
            key = "overlap_ms" if active["model"] and active["tool"] else "model_only_ms" if active["model"] else "tool_only_ms" if active["tool"] else "gap_ms"
            parts[key] += point - previous
        for kind in active:
            active[kind] += change[kind]
        previous = point
    return dict(
        **parts,
        wall_span_ms=max(changes) - min(changes) if changes else None,
        start_ms=min(changes) if changes else None,
        end_ms=max(changes) if changes else None,
        measured_calls=measured,
        missing_intervals=len(calls) - measured,
        basis="recorded_timestamps; gaps are unobserved intervals, not proven idle time",
    )


def _preview(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit], len(text) > limit


def _unwrap(value):
    for _ in range(5):
        if not isinstance(value, Mapping):
            return value
        nested = next((value[k] for k in ("value", "output", "data") if isinstance(value.get(k), Mapping)), None)
        if nested is None:
            break
        value = nested
    return value


def execution_view(source, bundle):
    atoms = bundle["cost_ledger"]["atoms"]
    assignments = {r["cost_atom_id"]: r for r in bundle["cost_ledger"]["assignments"]}
    rows, proposed = [], defaultdict(list)
    for atom in atoms:
        refs = atom.get("evidence_refs", [])
        events = [source.event(ref) for ref in refs]
        start = next((e for e in events if e.event_type.endswith((".requested", ".started"))), None)
        end = next((e for e in reversed(events) if e.event_type.endswith((".completed", ".failed", ".cancelled", ".interrupted"))), None)
        row = {
            k: atom.get(k)
            for k in (
                "id",
                "kind",
                "model",
                "tool",
                "role",
                "stage",
                "family",
                "status",
                "duration_ms",
                "started_at",
                "ended_at",
                "ttft_ms",
                "tps",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "usage_status",
                "duration_basis",
            )
        }
        row.update(
            start_line=min((r["line_number"] for r in refs), default=0),
            end_line=max((r["line_number"] for r in refs), default=0),
            evidence_refs=refs,
            owner=assignments.get(atom["id"], {}).get("owner", "analysis:unassigned"),
            related_ids=assignments.get(atom["id"], {}).get("related_ids", []),
            caller_id=None,
        )
        row["category"] = "model" if atom["kind"] == "model" else atom.get("family") or "tool"
        if atom["kind"] == "model":
            response = end.payload.get("response", {}) if end else {}
            if isinstance(response, Mapping):
                row["stream_metrics"] = response.get("stream_metrics")
                content = response.get("content")
                row["statement"], row["statement_truncated"] = _preview(content or "", 1600)
                row["statement_refs"] = [asdict(source.ref(end, "response.content", start=0, end=len(row["statement"])))] if content else []
                for tool in response.get("tool_calls") or []:
                    if isinstance(tool, Mapping) and tool.get("id") and end:
                        proposed[(end.run_id, end.payload.get("trace_id"), tool["id"])].append(row)
        else:
            inp = start.payload.get("input", {}) if start else {}
            row["input"], row["input_truncated"] = _preview(inp, 1000)
            fields = _unwrap(inp)
            row["target"] = next(
                (str(fields[k])[:240] for k in ("path", "file_path", "query", "command", "url") if isinstance(fields, Mapping) and fields.get(k)), ""
            )
            row["target_key"] = next((k for k in ("path", "file_path", "query", "command", "url") if isinstance(fields, Mapping) and fields.get(k)), None)
            row["input_refs"] = [asdict(source.ref(start, "input"))] if start and "input" in start.payload else []
            value = end.payload.get("output", end.payload.get("error", {})) if end else None
            observed = _unwrap(value)
            if isinstance(observed, Mapping):
                observed = next((observed[k] for k in ("content", "text", "stdout", "message") if observed.get(k)), observed)
            row["observation"], row["observation_truncated"] = _preview(observed, 1000) if end else ("No terminal event recorded", False)
            field = "output" if end and "output" in end.payload else "error" if end and "error" in end.payload else None
            row["observation_refs"] = [asdict(source.ref(end, field))] if field else []
        rows.append(row)
    by_id = {r["id"]: r for r in rows}
    nodes, edges, rounds = [], [], []
    for atom in atoms:
        row = by_id[atom["id"]]
        if row["kind"] != "tool":
            continue
        event = next((source.event(r) for r in atom["evidence_refs"] if source.event(r).event_type == "tool.started"), None)
        if event:
            candidates = proposed.get((event.run_id, event.payload.get("trace_id"), atom.get("tool_call_id")), [])
            candidates = [m for m in candidates if m["end_line"] <= row["start_line"]]
            if len(candidates) == 1:
                row["caller_id"] = candidates[0]["id"]
    for row in rows:
        identifier = row["id"]
        if row["kind"] == "model":
            nodes.append(dict(id=identifier, kind="public_statement", call_id=identifier, evidence_refs=row.get("statement_refs", [])))
            rounds.append(
                dict(
                    id=identifier,
                    model_call_id=identifier,
                    tool_call_ids=[r["id"] for r in rows if r.get("caller_id") == identifier],
                    start_line=row["start_line"],
                )
            )
        else:
            nodes.append(dict(id=identifier, kind="action", call_id=identifier, evidence_refs=row.get("input_refs", [])))
            if row.get("caller_id"):
                edges.append(dict(source_id=row["caller_id"], target_id=identifier, kind="invokes", basis="recorded_tool_call_id"))
            else:
                rounds.append(dict(id=identifier, model_call_id=None, tool_call_ids=[identifier], start_line=row["start_line"]))
            if row.get("observation_refs"):
                nodes.append(dict(id=identifier + ":result", kind="observation", call_id=identifier, evidence_refs=row["observation_refs"]))
                edges.append(dict(source_id=identifier, target_id=identifier + ":result", kind="returns", basis="recorded_call_pair"))
    rounds.sort(key=lambda r: r["start_line"])
    for a, b in zip(rounds, rounds[1:], strict=False):
        edges.append(dict(source_id=a["id"], target_id=b["id"], kind="source_order", basis="archive_order_not_causal_adoption"))

    objects = {r["id"]: r for k in ("goals", "attempts", "phases", "activities") for r in bundle[k] if not r.get("superseded_by")}

    def descendants(identifier):
        found = {identifier}
        while True:
            more = found | {i for i, r in objects.items() if (r.get("parent_goal_id") or r.get("goal_id") or r.get("attempt_id")) in found}
            if more == found:
                return found
            found = more

    def scope(calls, related):
        assigned = [r for r in calls if r["id"] in related]
        unrelated = [r for r in calls if r["id"] not in related]
        return dict(
            call_ids=[r["id"] for r in calls],
            related_call_ids=[r["id"] for r in assigned],
            other_call_ids=[r["id"] for r in unrelated],
            unassigned_call_ids=[r["id"] for r in calls if r["owner"] == "analysis:unassigned"],
            timing=time_partition(calls),
            related_timing=time_partition(assigned),
            costs=summarize(calls),
            related_costs=summarize(assigned),
        )

    phases, goals = {}, {}
    for phase in bundle["phases"]:
        if phase.get("superseded_by"):
            continue
        related = set(phase.get("call_ids", []))
        linked = [r for r in rows if r["id"] in related]
        low = min([phase["start_line"]] + [r["start_line"] for r in linked])
        high = max([phase["end_line"]] + [r["end_line"] for r in linked])
        calls = [r for r in rows if r["start_line"] <= high and r["end_line"] >= low]
        phases[phase["id"]] = dict(**scope(calls, related), start_line=low, end_line=high, basis="phase_evidence_window_not_ownership")
    for goal in bundle["goals"]:
        if goal.get("superseded_by"):
            continue
        related = {a["cost_atom_id"] for a in assignments.values() if a["owner"] in descendants(goal["id"])}
        if not goal.get("parent_goal_id"):
            calls = [r for r in rows if r["start_line"] <= goal["end_line"] and r["end_line"] >= goal["origin_line"]]
            basis = "effective_root_revision_window"
        else:
            calls = [r for r in rows if r["id"] in related]
            basis = "attributed_calls_only"
        goals[goal["id"]] = dict(**scope(calls, related), basis=basis)
    in_windows = {i for s in phases.values() for i in s["call_ids"]}
    run = scope(rows, {r["id"] for r in rows if r["owner"] != "analysis:unassigned"})
    run["outside_phase_call_ids"] = [r["id"] for r in rows if r["id"] not in in_windows]
    from loom.evaluation.goal_loops.action_flow import action_flow

    return dict(
        action_flow=action_flow(rows, rounds),
        version=VERSION,
        basis="fixed_source_projection_no_model_call",
        calls=rows,
        nodes=nodes,
        edges=edges,
        rounds=rounds,
        phases=phases,
        goals=goals,
        run=run,
    )
