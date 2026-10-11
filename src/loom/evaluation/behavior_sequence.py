"""Recorded cross-step activity. No workspace reads and no semantic progress claims."""

import hashlib
import posixpath
import shlex
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict

from loom.core import thaw_json
from loom.evaluation.behavior_contracts import stable_id
from loom.evaluation.evidence_store import text_value

PLAN_EVENTS = {"plan.entered", "plan.submitted", "plan.updated", "plan.completed", "workflow.revised",
               "workflow.node.started", "workflow.node.completed"}


def plan_snapshot(store, event, episode_id, previous):
    p = event.payload
    workflow = p.get("workflow", {})
    workflow = workflow if isinstance(workflow, Mapping) else {}
    definition = p.get("plan") or workflow.get("workflow", workflow)
    definition = definition if isinstance(definition, Mapping) else {}
    nodes = [{"id": n.get("id"), "description": n.get("content") or n.get("objective") or n.get("step")
              or n.get("description") or n.get("title"),
              "dependencies": thaw_json(n.get("dependencies", n.get("depends_on", ()))),
              "declared_status": n.get("status", "unknown"), "evidenced_done": "unknown"}
             for n in definition.get("items", definition.get("nodes", ())) if isinstance(n, Mapping)]
    # Plan items may omit workflow dependency information.
    dependency_nodes = workflow.get("workflow", workflow).get("nodes", ())
    deps = {n.get("id"): thaw_json(n.get("dependencies", ())) for n in dependency_nodes if isinstance(n, Mapping)}
    for n in nodes:
        n["dependencies"] = n["dependencies"] or deps.get(n["id"], [])
    active = workflow.get("active")
    active_ids = [active] if isinstance(active, str) else [n["id"] for n in nodes if n["declared_status"] in {"in_progress", "running"}]
    return {
        "id": stable_id("plan", store.source_sha256, event.line_number), "episode_id": episode_id,
        "plan_id": p.get("plan_id") or definition.get("id") or (previous or {}).get("plan_id") or stable_id("workflow", episode_id),
        "kind": event.event_type, "line": event.line_number,
        "revision": p.get("revision", p.get("plan_revision", definition.get("revision"))),
        "state": "declared_complete" if event.event_type in {"plan.completed", "workflow.node.completed"}
        else "proposed" if event.event_type == "plan.entered" else "accepted",
        "acceptance_basis": "planning_started" if event.event_type == "plan.entered" else "runtime_transition",
        "evidenced_done": "unknown", "definition": thaw_json(p), "nodes": nodes, "active_node_ids": active_ids,
        "supersedes": previous["id"] if previous else None, "evidence_refs": [asdict(store.ref(event))],
    }


def bind_execution(plans, calls, tools):
    rows = {}
    for plan in plans:
        for node in plan["nodes"]:
            key = plan["episode_id"], plan["plan_id"], node["id"]
            row = rows.setdefault(key, {"id": stable_id("execution", *key), "episode_id": key[0], "plan_id": key[1],
                                      "node_id": key[2], "round_ids": [], "tool_ids": [], "steps": [], "revisions": [],
                                      "evidence_refs": [], "evidenced_done": "unknown", "association_basis": "active_plan_snapshot"})
            row.update(description=node["description"], dependencies=node["dependencies"], declared_status=node["declared_status"])
            row["revisions"].append(plan["id"])
            row["evidence_refs"].extend(plan["evidence_refs"])
    for call in calls:
        line = (call.get("request_ref") or call.get("response_ref") or {}).get("line_number", 0)
        active = next((p for p in reversed(plans) if p["episode_id"] == call["episode_id"] and p["line"] <= line and p["nodes"]), None)
        call["plan_snapshot_id"] = active["id"] if active else None
        call["plan_node_id"] = active["active_node_ids"][0] if active and len(active["active_node_ids"]) == 1 else None
        if call["usage_role"] == "verification" or call["plan_node_id"] is None:
            continue
        row = rows.get((call["episode_id"], active["plan_id"], call["plan_node_id"]))
        if row is None:
            continue
        row["round_ids"].append(call["id"])
        row["tool_ids"].extend(t["id"] for t in tools if t["round_id"] == call["id"] and t["usage_role"] != "verification")
        step = {"run_id": call["run_id"], "trace_id": call["trace_id"], "step_number": call["step_number"]}
        if step not in row["steps"]:
            row["steps"].append(step)
    return list(rows.values())


def _unwrap(value):
    while isinstance(value, Mapping) and "value" in value and ("ok" in value or "source" in value):
        value = value["value"]
    return thaw_json(value) if isinstance(value, Mapping) else {}


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path(path):
    return posixpath.normpath(path) if isinstance(path, str) and path else None


def _resource(tool, inp, out):
    meta = out.get("resource", {})
    name = tool["tool_id"]
    path = _path(meta.get("path") or out.get("path") or inp.get("path"))
    operation = "read" if name == "read_file" else "write" if name in {"write_file", "edit_file", "create_report"} else None
    if name == "shell_execute":
        # Only the unambiguous single-file cat form. Never execute or broadly parse shell.
        try:
            words = shlex.split(inp.get("command", ""), posix=True)
        except ValueError:
            words = []
        if len(words) == 2 and words[0] == "cat" and not words[1].startswith("-") and not any(c in words[1] for c in "$`*?;|&<>\n"):
            path = _path(posixpath.join(out.get("resolved_cwd") or inp.get("cwd") or ".", words[1]))
            operation = "read"
    if operation is None or path is None:
        return None
    content = out.get("content") if name == "read_file" else out.get("stdout") if operation == "read" else None
    truncated = any(out.get(k) is True for k in ("truncated", "stdout_truncated", "output_truncated"))
    digest = meta.get("returned_sha256") or (_hash(content) if isinstance(content, str) else None)
    full = meta.get("after_sha256") or (digest if operation == "read" and not truncated else None)
    return {"path": path, "operation": operation, "content_sha256": digest, "version_sha256": full,
            "before_sha256": meta.get("before_sha256"), "before_state": meta.get("before_state"), "range": meta.get("byte_range") or
            ([0, len(content.encode("utf-8"))] if isinstance(content, str) else None), "truncated": truncated,
            "identity_basis": "recorded_canonical_path" if meta.get("path") else "recorded_lexical_path",
            "version_basis": "recorded_bytes" if meta.get("after_sha256") else "returned_text" if full else "partial_or_missing"}


def build_sequence(store, plans, calls, tools, segments, by_line, *, low_progress_calls=10):
    execution = bind_execution(plans, calls, tools)
    call_map = {c["id"]: c for c in calls}
    owners = {r: s["id"] for s in segments for r in s["round_ids"]}
    resources, repeats, signals, windows = [], [], [], []
    latest, writes, barriers, outputs, pending = {}, defaultdict(int), defaultdict(int), set(), defaultdict(list)
    mutations = []
    for t in tools:
        if t["tool_id"] not in {"write_file", "edit_file", "create_report", "shell_execute", "process_execute"}:
            continue
        inp = _unwrap(store.resolve(t["input_ref"])) if t.get("input_ref") else {}
        out = _unwrap(store.resolve(t["raw_output_ref"])) if t.get("raw_output_ref") else {}
        resource = _resource(t, inp, out)
        if resource and resource["operation"] == "read":
            continue
        refs = [r for r in (t.get("input_ref"), t.get("raw_output_ref")) if r]
        if refs:
            mutations.append((min(r["line_number"] for r in refs), max(r["line_number"] for r in refs),
                              resource["path"] if resource else None, by_line.get(refs[0]["line_number"])))

    def signal(kind, line, ep, label, refs, **extra):
        row = {"id": stable_id("signal", kind, line, label), "kind": kind, "line": line, "episode_id": ep,
               "label": label, "evidence_refs": refs, "basis": "recorded", "semantic_progress": "not_assessed", **extra}
        signals.append(row)
        return row

    for t in sorted(tools, key=lambda t: (t.get("raw_output_ref") or t.get("input_ref") or {}).get("line_number", 0)):
        call = call_map.get(t["round_id"], {})
        ref = t.get("raw_output_ref") or t.get("input_ref")
        if ref is None or t["usage_role"] == "verification":
            continue
        line = ref["line_number"]
        ep = call.get("episode_id") or by_line.get(line)
        scope = ep, call.get("goal_revision_id")
        inp = _unwrap(store.resolve(t["input_ref"])) if t.get("input_ref") else {}
        out = _unwrap(store.resolve(t["raw_output_ref"])) if t.get("raw_output_ref") else {}
        resource = _resource(t, inp, out) if t["status"] == "complete" else None
        changed = False
        if resource:
            resource.update(id=stable_id("resource", t["id"]), tool_call_id=t["id"], round_id=t["round_id"], episode_id=ep,
                            goal_revision_id=scope[1], step_number=t["step_number"], line=line, evidence_refs=[ref])
            resources.append(resource)
            key = scope, resource["path"]
            if resource["operation"] == "write":
                writes[key] += 1
                before_known = resource["before_sha256"] is not None or resource["before_state"] == "absent"
                if before_known and resource["version_sha256"] and resource["before_sha256"] != resource["version_sha256"]:
                    signal("artifact_changed", line, ep, resource["path"], [ref], round_id=t["round_id"], step_number=t["step_number"])
                    changed = True
            elif resource["content_sha256"]:
                prev = latest.get(key)
                stamp = (writes[key], barriers[scope])
                equal = prev and all(prev[0][k] == resource[k] for k in ("content_sha256", "range", "version_sha256"))
                if equal:
                    before, previous_stamp = prev
                    reasons = []
                    overlapping = any(a <= line and b >= before["line"] and mep == ep and (path is None or path == resource["path"])
                                      for a, b, path, mep in mutations)
                    if previous_stamp != stamp or overlapping:
                        reasons.append("Recorded write or opaque command between observations")
                    if resource["truncated"] or before["truncated"]:
                        reasons.append("Only a truncated range was returned")
                    repeated = {"id": stable_id("repeat", before["id"], resource["id"]), "path": resource["path"],
                                "episode_id": ep, "goal_revision_id": scope[1], "first_line": before["line"], "line": line,
                                "tool_ids": [before["tool_call_id"], t["id"]], "round_ids": [before["round_id"], t["round_id"]],
                                "status": "candidate" if not reasons else "needs_context", "limitations": reasons,
                                "basis": "Same recorded content and range; matching snapshots do not establish continuous immutability",
                                "evidence_refs": before["evidence_refs"] + [ref]}
                    repeats.append(repeated)
                    sids = list(dict.fromkeys(owners[r] for r in repeated["round_ids"] if r in owners))
                    if sids:
                        windows.append({"id": repeated["id"], "pattern_type": "repeated_unchanged_result", "segment_ids": sids,
                                        "status": "candidate", "basis": repeated["basis"], "evidence_refs": repeated["evidence_refs"]})
                else:
                    signal("resource_observed", line, ep, resource["path"], [ref], round_id=t["round_id"], step_number=t["step_number"])
                    changed = True
                latest[key] = resource, stamp
        elif t["tool_id"] in {"shell_execute", "process_execute", "write_file", "edit_file", "create_report"}:
            barriers[scope] += 1
        # Compare useful payload, not volatile process duration/observation IDs.
        result = {k: out[k] for k in ("stdout", "stderr", "exit_code", "content", "results") if k in out}
        signature = (scope, t["tool_id"], text_value(result))
        if resource is None and result and signature not in outputs:
            signal("tool_result_observed", line, ep, t["tool_id"], [ref], round_id=t["round_id"], step_number=t["step_number"])
            changed = True
        outputs.add(signature)
        if changed:
            pending[scope] = []
        else:
            pending[scope].append(t)
            if len(pending[scope]) >= low_progress_calls:
                group = pending[scope]
                sids = list(dict.fromkeys(owners[x["round_id"]] for x in group if x["round_id"] in owners))
                windows.append({"id": stable_id("window", *(x["id"] for x in group)), "pattern_type": "low_progress_window",
                                "segment_ids": sids, "status": "candidate", "tool_calls": len(group),
                                "round_ids": list(dict.fromkeys(x["round_id"] for x in group)),
                                "basis": "No new recognizable recorded result in this window; semantic progress may still exist",
                                "evidence_refs": [x.get("raw_output_ref") or x["input_ref"] for x in group]})
                pending[scope] = []
    for p in plans:
        signal("plan_snapshot", p["line"], p["episode_id"], p["kind"], p["evidence_refs"])
    for e in store.events:
        if e.event_type in {"verification.completed", "verification.recorded", "acceptance.gate.passed", "acceptance.gate.blocked"}:
            result = e.payload.get("result", {})
            result = result if isinstance(result, Mapping) else {}
            label = result.get("reason") or e.payload.get("reason") or e.event_type
            signal("verification_result", e.line_number, by_line.get(e.line_number), str(label), [asdict(store.ref(e))])
    return {"schema_version": "loom.behavior-sequence.v1", "plan_execution": execution, "resources": resources,
            "repetitions": repeats, "signals": sorted(signals, key=lambda s: s["line"]), "candidate_windows": windows,
            "low_progress_tool_threshold": low_progress_calls,
            "limitations": ["Activity signals are not semantic progress judgments.",
                            "Historical/opaque operations may lack resource versions or execution-plan associations."]}


def focus_context(facts, segment, *, max_neighbors=4):
    """Bound navigation context; only the primary segment owns assessment coverage."""
    segments = facts["segments"]
    index = next(i for i, s in enumerate(segments) if s["id"] == segment["id"])
    linked = [sid for w in facts["candidate_windows"] if segment["id"] in w["segment_ids"] for sid in w["segment_ids"]]
    neighbors = list(dict.fromkeys(linked + [s["id"] for s in segments[max(0, index - 1):index + 2]]))
    eligible = [s for s in segments if s["id"] in neighbors and s["id"] != segment["id"]
                and s["episode_id"] == segment["episode_id"] and s["goal_revision_id"] == segment["goal_revision_id"]]
    selected = eligible[:max_neighbors]
    round_ids = {r for s in [segment, *selected] for r in s["round_ids"]}
    calls = [c for c in facts["model_calls"] if c["id"] in round_ids]
    plan_ids = {c.get("plan_snapshot_id") for c in calls}
    sequence = facts.get("sequence", {})
    plans = [{k: p[k] for k in ("id", "plan_id", "revision", "line", "nodes", "active_node_ids", "evidence_refs")}
             for p in facts["plan_revisions"] if p["id"] in plan_ids]
    signals = [s for s in sequence.get("signals", []) if s.get("round_id") in round_ids]
    primary_plans = {c.get("plan_snapshot_id") for c in calls if c["id"] in segment["round_ids"]} - {None}
    return {"context_segments": selected, "plans": plans, "activity_signals": signals[:24],
            "round_plan_bindings": [{"round_id": c["id"], "plan_snapshot_id": c.get("plan_snapshot_id"),
                                     "plan_node_id": c.get("plan_node_id")} for c in calls],
            "context_limitations": ["Context segments are navigation, not reviewed coverage; read source evidence before citing."]
            + (["No recorded execution plan is bound to the primary segment; return plan_assessments: []. "
                "This does not establish that planning was absent or inadequate."] if not primary_plans else [])
            + (["Additional linked context omitted by window limit"] if len(eligible) > len(selected) else [])}
