"""Offline goal/plan/behavior ledgers. Never inspect the current workspace."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime

from loom.core import thaw_json
from loom.evaluation.behavior_contracts import ANALYZER_VERSION, outcome, stable_id
from loom.evaluation.behavior_goals import request_goals
from loom.evaluation.context_analysis import request_fields
from loom.evaluation.evidence_store import text_value
from loom.evaluation.trajectory import build_fact_analysis
from loom.trace_analysis import build_episode_graph


def build_behavior_facts(store, *, legacy=None, task=None):
    graph = build_episode_graph(store.events)
    legacy = legacy or build_fact_analysis(store, graph, task=task)
    episodes, goals, plans, artifacts, receipts = [], [], [], [], []
    current, pending, closed = {}, {}, set()
    by_line, visible_messages = {}, set()

    def episode(event):
        key = event.run_id
        explicit = event.payload.get("task_episode_id")
        existing = next((row for row in episodes if row["id"] == explicit), None) if explicit else None
        if existing:
            current[key] = existing
            closed.discard(key)
        # Session bookkeeping and inputs awaiting application are not executions.
        execution_event = event.event_type.startswith(("llm.", "tool.", "run.", "plan.", "workflow.", "acceptance.", "verification.")) or event.event_type in {
            "task.goal.revised",
            "verification.recorded",
            "artifact.version.recorded",
        }
        if key is None and (not execution_event or event.event_type == "task.goal.revised"):
            return None
        if event.event_type == "message.created" and event.payload.get("role") == "user" and key in closed:
            return None
        if key not in current and not execution_event:
            return None
        if key not in current or (explicit and current[key]["id"] != explicit) or (event.event_type == "run.started" and key in closed):
            row = {
                "id": explicit or stable_id("episode", store.source_sha256, key, event.line_number),
                "run_id": event.run_id,
                "start_line": event.line_number,
                "end_line": event.line_number,
                "started_at": event.at,
                "ended_at": event.at,
                "execution_status": "unknown",
                "identity_basis": "recorded" if explicit else "inferred_boundary",
                "attempts": [],
            }
            current[key] = row
            episodes.append(row)
            closed.discard(key)
        row = current[key]
        row["end_line"] = event.line_number
        if event.event_type.startswith(("run.", "llm.", "tool.")):
            row["ended_at"] = event.at
        if event.event_type == "run.started":
            row["attempts"].append({"id": event.payload.get("attempt_id"), "line": event.line_number})
        status = (
            event.payload.get("state")
            if event.event_type == "run.state.changed"
            else {
                "run.started": "running",
                "run.completed": "completed",
                "run.failed": "failed",
                "run.stopped": "stopped",
                "run.paused": "paused",
                "run.suspended": "suspended",
            }.get(event.event_type)
        )
        if status:
            row["execution_status"] = status
            if status in {"running", "suspended", "paused", "queued"}:
                closed.discard(key)
        if status == "completed" or (event.event_type == "message.created" and event.payload.get("role") == "assistant"):
            closed.add(key)
        return row

    def goal(ep, event, objective, origin, state, *, full=False, criteria=(), command_id=None, ref=None):
        if not isinstance(objective, str) or not objective.strip():
            return None
        ref = ref or asdict(store.ref(event))
        episode_id = ep["id"] if ep else None
        if ep is None:
            state = "accepted"
        previous = next(
            (g for g in reversed(goals) if episode_id and g["episode_id"] == episode_id and g["state"] == "applied" and not g.get("covered_by_revision")), None
        )
        identifier = stable_id("goal", store.source_sha256, episode_id, event.line_number, origin, objective)
        explicit = [
            {
                "id": f"{identifier}:criterion:{i}",
                "description": c if isinstance(c, str) else c.get("description", ""),
                "required": True if isinstance(c, str) else c.get("required", True),
                "source_id": None if isinstance(c, str) else c.get("id"),
                "origin": origin,
                "status": "unverified",
            }
            for i, c in enumerate(criteria)
            if isinstance(c, (str, Mapping))
        ]
        # A natural-language correction is retained verbatim, not silently substituted for the complete goal.
        if previous and not full and state == "applied":
            explicit = [dict(c, id=f"{identifier}:inherited:{i}", inherited_from=c["id"]) for i, c in enumerate(previous["criteria"])] + explicit
        explicit.append({"id": f"{identifier}:objective", "description": objective, "required": True, "origin": origin, "status": "unverified"})
        row = {
            "id": identifier,
            "episode_id": episode_id,
            "objective": objective,
            "origin": origin,
            "state": state,
            "producer_revision": event.payload.get("goal_revision"),
            "accepted_line": event.line_number,
            "applied_line": event.line_number if state == "applied" else None,
            "visible_line": None,
            "command_id": command_id,
            "criteria": explicit,
            "evidence_refs": [ref],
            "supersedes": previous["id"] if previous and state == "applied" else None,
            "goal_coverage": "complete" if full else "unknown",
        }
        goals.append(row)
        return row

    for event in store.events:
        p = event.payload
        ep = episode(event)
        if ep:
            by_line[event.line_number] = ep["id"]
        if event.event_type == "message.created" and p.get("role") == "user":
            row = goal(ep, event, p.get("content"), "user_message", "accepted", command_id=p.get("command_id"))
            if row and p.get("command_id"):
                pending[p["command_id"]] = row
        elif event.event_type == "command.applied" and p.get("command_id") in pending and ep:
            row = pending[p["command_id"]]
            row.update(state="applied", applied_line=row["applied_line"] or event.line_number, episode_id=ep["id"])
            revisions = [g for g in goals if g is not row and g["episode_id"] == ep["id"] and g["origin"] == "goal_revision"]
            covering = next(
                (
                    g
                    for g in reversed(revisions)
                    if g["objective"] == row["objective"] or p.get("command_id") in store.event(g["evidence_refs"][0]).payload.get("applied_command_ids", ())
                ),
                None,
            )
            if covering:
                covering.update(state="applied", applied_line=covering["applied_line"] or event.line_number)
                row["covered_by_revision"] = covering["id"]
            else:
                prior = next(
                    (
                        g
                        for g in reversed(goals)
                        if g is not row and g["episode_id"] == ep["id"] and g["state"] == "applied" and not g.get("covered_by_revision")
                    ),
                    None,
                )
                if prior:
                    row["supersedes"] = prior["id"]
                    row["criteria"] = [dict(c) for c in prior["criteria"]] + row["criteria"]
                else:
                    row["goal_coverage"] = "complete"
        elif event.event_type == "task.goal.revised":
            goal(
                ep,
                event,
                p.get("objective"),
                "goal_revision",
                "applied" if p.get("input_cursor") is not None else "accepted",
                full=True,
                criteria=p.get("criteria", ()),
            )
        elif event.event_type in {"task.created", "run.started"}:
            definition = p.get("task", p.get("metadata", p))
            if isinstance(definition, Mapping):
                goal(ep, event, definition.get("objective"), "task_definition", "applied", full=True, criteria=definition.get("success_criteria", ()))
        if event.event_type == "llm.requested" and p.get("usage_role") != "verification" and ep:
            messages, path, _, _ = request_fields(event)
            eligible = [g for g in goals if g["episode_id"] == ep["id"] or (g["episode_id"] is None and g["state"] == "accepted")]
            candidates = request_goals(
                messages, path, known={g["objective"] for g in eligible}, allow_plain=not any(g["origin"] in {"user_message", "task_definition"} for g in goals)
            )
            for content, field, start, end, full, origin in candidates:
                matches = [g for g in eligible if g["objective"] == content]
                if matches:
                    for match in matches:
                        if match["visible_line"] is None:
                            match.update(
                                visible_line=event.line_number, episode_id=ep["id"], visibility_ref=asdict(store.ref(event, field, start=start, end=end))
                            )
                        if match["state"] == "accepted":
                            match.update(state="applied", applied_line=event.line_number)
                        if full:
                            match["goal_coverage"] = "complete"
                elif (ep["id"], content) not in visible_messages:
                    row = goal(
                        ep,
                        event,
                        content,
                        origin,
                        "applied",
                        full=full or not any(g["episode_id"] == ep["id"] and g["state"] == "applied" for g in goals),
                        ref=asdict(store.ref(event, field, start=start, end=end)),
                    )
                    if row:
                        row["visible_line"] = event.line_number
                visible_messages.add((ep["id"], content))
        if event.event_type in {"plan.submitted", "plan.updated", "workflow.revised", "plan.entered", "workflow.node.completed"}:
            plans.append(
                {
                    "id": stable_id("plan", store.source_sha256, event.line_number),
                    "episode_id": ep["id"],
                    "kind": event.event_type,
                    "line": event.line_number,
                    "revision": p.get("revision", p.get("plan_revision")),
                    "state": "declared_complete"
                    if event.event_type == "workflow.node.completed"
                    else "accepted"
                    if event.event_type in {"plan.submitted", "plan.updated", "workflow.revised"}
                    else "proposed",
                    "acceptance_basis": "runtime_transition" if event.event_type != "plan.entered" else "planning_started",
                    "evidenced_done": "unknown",
                    "definition": thaw_json(p),
                    "evidence_refs": [asdict(store.ref(event))],
                }
            )
            plan = plans[-1]
            previous = next((r for r in reversed(plans[:-1]) if r["episode_id"] == ep["id"]), None)
            plan["supersedes"] = previous["id"] if previous else None
            definition = p.get("plan", p.get("workflow", {}))
            plan["nodes"] = [
                {
                    "id": n.get("id"),
                    "description": n.get("step", n.get("description", n.get("title"))),
                    "dependencies": thaw_json(n.get("dependencies", n.get("depends_on", ()))),
                    "declared_status": n.get("status", "unknown"),
                    "evidenced_done": "unknown",
                }
                for n in definition.get("items", definition.get("nodes", ()))
                if isinstance(n, Mapping)
            ]
        if event.event_type in {"artifact.version.recorded", "verification.recorded"}:
            row = {**thaw_json(p), "episode_id": ep["id"], "line": event.line_number, "evidence_refs": [asdict(store.ref(event))]}
            (artifacts if event.event_type == "artifact.version.recorded" else receipts).append(row)
    if task and store.events:
        for ep in episodes:
            event = next(e for e in store.events if by_line.get(e.line_number) == ep["id"])
            goal(ep, event, task, "explicit_task", "applied", full=True)

    calls = []
    event_by_line = {e.line_number: e for e in store.events}
    for row in legacy.trajectory:
        ref = row.get("request_ref") or row.get("response_ref")
        line = ref["line_number"] if ref else None
        ep_id = by_line.get(line)
        applied = [
            g
            for g in goals
            if g["episode_id"] == ep_id and g["applied_line"] is not None and g["applied_line"] <= (line or 0) and not g.get("covered_by_revision")
        ]
        active = max(applied, key=lambda g: g["applied_line"], default=None)
        request_event = event_by_line.get(line)
        role = request_event.payload.get("usage_role", "solver") if request_event else "solver"
        calls.append({**row, "episode_id": ep_id, "goal_revision_id": active["id"] if active else None, "usage_role": role})
    tools_with_roles = []
    for tool in legacy.tool_uses:
        refs = tool.get("evidence_refs", [])
        event = store.event(refs[0]) if refs else None
        role = event.payload.get("metadata", {}).get("usage_role", "solver") if event else "solver"
        tools_with_roles.append({**tool, "usage_role": role})
    segments = []
    for row in calls:
        if row["usage_role"] == "verification":
            continue
        # Contiguous bounded step windows own cost once, independent of overlapping candidate windows.
        tools = [t for t in tools_with_roles if t["round_id"] == row["id"] and t["usage_role"] != "verification"]
        refs = row["evidence_refs"] + [r for t in tools for r in t["evidence_refs"]]
        key = (row["episode_id"], row["goal_revision_id"], row["loop_id"], row["trace_id"], row["step_number"])
        if segments and segments[-1]["boundary_key"] == key and len(segments[-1]["round_ids"]) < 4:
            segments[-1]["round_ids"].append(row["id"])
            segments[-1]["evidence_refs"].extend(refs)
        else:
            segments.append(
                {
                    "id": stable_id("segment", row["id"]),
                    "episode_id": row["episode_id"],
                    "goal_revision_id": row["goal_revision_id"],
                    "round_ids": [row["id"]],
                    "boundary_key": key,
                    "evidence_refs": refs,
                    "cost_owner": True,
                    "state_change": "unknown",
                }
            )
        segments[-1]["id"] = stable_id("segment", *segments[-1]["round_ids"])
        segments[-1].setdefault("navigation", []).append(
            {
                "round_id": row["id"],
                "response_excerpt": store.read(row["response_ref"], max_chars=400)["content"] if row.get("response_ref") else None,
                "tools": [{"tool_id": tool["tool_id"], "status": tool["status"], "output_excerpt": tool["output_excerpt"][:200]} for tool in tools],
                "excerpt_only": True,
            }
        )
    candidates = []
    signatures = defaultdict(list)
    for tool in tools_with_roles:
        if tool["usage_role"] == "verification":
            continue
        if tool.get("input_ref") and tool.get("raw_output_ref"):
            key = (
                next((r["episode_id"] for r in calls if r["id"] == tool["round_id"]), None),
                tool["tool_id"],
                text_value(store.resolve(tool["input_ref"])),
                text_value(store.resolve(tool["raw_output_ref"])),
            )
            signatures[key].append(tool)
    for values in signatures.values():
        if len(values) > 1:
            ids = {t["round_id"] for t in values}
            candidates.append(
                {
                    "pattern_type": "repeated_unchanged_result",
                    "segment_ids": [s["id"] for s in segments if ids.intersection(s["round_ids"])],
                    "status": "candidate",
                    "basis": "Identical recorded input and output; justified retries may be effective.",
                }
            )
    activity = []
    for tool in legacy.tool_uses:
        name = (tool.get("tool_id") or "").lower()
        kind = "plan" if "plan" in name else "search" if any(k in name for k in ("search", "find", "grep")) else None
        if kind:
            owner = next((segment for segment in segments if tool["round_id"] in segment["round_ids"]), None)
            if owner:
                activity.append((kind, owner))
    for i in range(max(0, len(activity) - 3)):
        window = activity[i : i + 4]
        if [kind for kind, _ in window] in (["search", "plan", "search", "plan"], ["plan", "search", "plan", "search"]) and len(
            {segment["episode_id"] for _, segment in window}
        ) == 1:
            candidates.append(
                {
                    "pattern_type": "alternating_search_replan",
                    "status": "candidate",
                    "segment_ids": list(dict.fromkeys(segment["id"] for _, segment in window)),
                    "basis": "Alternating recorded tool families; inspect information gain and changed conditions before judging efficiency",
                }
            )
    for ep in episodes:
        try:
            ep["duration_ms"] = max(0, round((datetime.fromisoformat(ep["ended_at"]) - datetime.fromisoformat(ep["started_at"])).total_seconds() * 1000))
        except (TypeError, ValueError):
            ep["duration_ms"] = None
    checks = _outcomes(store, episodes, goals, artifacts, receipts)
    acceptance = [{"type": e.event_type, "episode_id": by_line.get(e.line_number), "line": e.line_number,
                   "state": thaw_json(e.payload.get("acceptance", {})), "assurance": e.payload.get("assurance"),
                   "evidence_refs": [asdict(store.ref(e))]} for e in store.events
                  if e.event_type.startswith("acceptance.") and e.payload.get("acceptance")]
    current_acceptance = next((row for row in reversed(acceptance) if episodes and row["episode_id"] == episodes[-1]["id"]), None)
    return {
        "analyzer_version": ANALYZER_VERSION,
        "episodes": episodes,
        "event_episode_ids": {str(line): ep for line, ep in by_line.items()},
        "goal_revisions": goals,
        "plan_revisions": plans,
        "artifact_versions": artifacts,
        "verification_receipts": receipts,
        "acceptance_history": acceptance,
        "model_calls": calls,
        "segments": segments,
        "candidate_windows": candidates,
        "tool_uses": tools_with_roles,
        "behavior_graph": {"nodes": [], "edges": []},
        "token_ledger": list(legacy.token_ledger),
        "base": {
            "metrics": {
                "model_calls": len(calls),
                "verification_model_calls": sum(r["usage_role"] == "verification" for r in calls),
                "tool_calls": len(legacy.tool_uses),
                "verification_tool_calls": sum(t["usage_role"] == "verification" for t in tools_with_roles),
                "steps": len(graph.steps),
                "episodes": len(episodes),
                "tokens": legacy.coverage.get("tokens", {}),
                "runtime_outcomes": [
                    {"type": e.event_type, "line": e.line_number, "run_id": e.run_id, "state": e.payload.get("state")}
                    for e in store.events
                    if e.event_type in {"run.completed", "run.failed", "run.stopped", "run.state.changed"}
                ],
                "duration_ms": sum(ep["duration_ms"] for ep in episodes) if episodes and all(ep["duration_ms"] is not None for ep in episodes) else None,
            },
            "execution_status": episodes[-1]["execution_status"] if episodes else "not_started",
            "output_checks": receipts,
            "runtime_acceptance": current_acceptance,
            "outcomes": checks,
            "task_completion": checks[-1]["status"] if checks else "unverified",
        },
        "coverage": {
            "source": thaw_json(store.coverage),
            "goal_ambiguity": any(g["goal_coverage"] != "complete" for g in goals),
            "artifact_binding": "recorded_receipts_only",
            "episode_identity": "mixed_recorded_and_inferred",
        },
    }


def _outcomes(store, episodes, goals, artifacts, receipts):
    results = []
    for ep in episodes:
        active = [g for g in goals if g["episode_id"] == ep["id"] and g["state"] == "applied" and not g.get("covered_by_revision")]
        if not active:
            results.append({"episode_id": ep["id"], "goal_revision_id": None, "goal_coverage": "unknown", "criteria": [], "status": "unverified"})
            continue
        g = max(active, key=lambda row: (row["applied_line"], row["origin"] == "goal_revision"))
        criteria = [dict(c) for c in g["criteria"]]
        for c in criteria:
            matches = [
                r
                for r in receipts
                if r["episode_id"] == ep["id"]
                and (
                    r.get("criterion_id") == c["id"]
                    or (
                        g.get("producer_revision") is not None
                        and r.get("goal_revision") == g["producer_revision"]
                        and r.get("criterion_id") == (c.get("source_id") or ("objective" if c["id"].endswith(":objective") else c["id"]))
                    )
                )
            ]
            for r in matches:
                valid = (
                    r.get("producer") == "loom.verification.v1"
                    and r.get("oracle")
                    and r.get("environment")
                    and r.get("scope")
                    and r.get("coverage") == "complete"
                    and r.get("status") in {"supported", "contradicted"}
                )
                versions = [a for a in artifacts if a["episode_id"] == ep["id"] and a.get("scope") == r.get("scope") and a["line"] > r["line"]]
                final = versions[-1] if versions else None
                manifest = r.get("before")
                manifest_valid = (
                    isinstance(manifest, dict)
                    and bool(manifest)
                    and isinstance(r.get("scope"), list)
                    and set(manifest) == set(r["scope"])
                    and all(isinstance(v, str) and len(v) == 64 for v in manifest.values())
                )
                bound = bool(
                    manifest_valid
                    and final
                    and r.get("before") == r.get("after") == final.get("manifest")
                    and final.get("exclusive") is True
                    and r.get("exclusive") is True
                    and r.get("environment") == final.get("environment")
                )
                # Arbitrary tools after a receipt may mutate untracked dependencies. A trusted final boundary is required.
                later_tools = [e for e in store.events if e.event_type == "tool.started" and final and final["line"] < e.line_number <= ep["end_line"]]
                if valid and bound and not later_tools and not store.coverage["hash_mismatches"]:
                    c.update(status=r["status"], oracle=r["oracle"], evidence_refs=r["evidence_refs"] + final["evidence_refs"], freshness="confirmed")
                else:
                    c.update(limitation="Verification lacks a complete oracle or applicable final artifact/environment binding", freshness="unknown")
        results.append(
            {
                "episode_id": ep["id"],
                "goal_revision_id": g["id"],
                "goal_coverage": g["goal_coverage"],
                "criteria": criteria,
                "status": outcome(criteria, goal_coverage=g["goal_coverage"]),
            }
        )
    for result, ep in zip(results, episodes, strict=True):
        active = next((g for g in goals if g["id"] == result["goal_revision_id"]), None)
        checks = [r for r in receipts if r["episode_id"] == ep["id"]]
        reason = (
            "No applied task goal was recorded for this execution."
            if not active
            else "No goal acceptance checks were recorded. Execution status and goal acceptance are reported separately."
            if not checks
            else "Recorded checks do not yet establish all requirements against the final output."
            if result["status"] == "unverified"
            else "Some requirements have supporting evidence; the remaining requirements need verification."
            if result["status"] == "partially_verified"
            else "Recorded acceptance checks support the required outcomes."
            if result["status"] == "achieved"
            else "Recorded acceptance checks contradict a required outcome."
        )
        result.update(
            objective=active["objective"] if active else None,
            execution_status=ep["execution_status"],
            duration_ms=ep["duration_ms"],
            explanation=reason,
            verification_count=len(checks),
        )
    return results
