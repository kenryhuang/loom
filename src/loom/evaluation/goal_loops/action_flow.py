"""Compact action-first execution stages, independent of semantic phase attribution."""

import re
from collections import Counter, defaultdict
from math import isclose

from loom.evaluation.base_metrics import timestamp

LABELS = dict(
    planning="Plan",
    research="Research",
    implementation="Edit / implement",
    verification="Verify",
    final_answer="Prepare final answer",
    mixed="Mixed actions",
    other="Other activity",
)


def classify(call):
    """Conservative recorded tool/stage rules; never classify by the result's prose."""
    name = (call.get("tool") or "").lower()
    stage = (call.get("stage") or "").lower()
    if call["kind"] == "model":
        if stage in {"plan", "planning", "acceptance_plan"}:
            return "planning", "Plan / acceptance setup", "recorded stage"
        if call.get("role") == "verification" or stage in {"verify", "verification"}:
            return "verification", "Acceptance verification", "recorded role / stage"
        return "other", "Model response", "no linked action"
    if name in {"finish", "create_report", "docs_output", "final_answer"}:
        return "final_answer", "Prepare report / final answer", "tool identity"
    if name in {"complete_node", "verify", "verify_task", "run_tests"}:
        return "verification", "Check completion" if name == "complete_node" else "Run verification", "tool identity"
    if name in {"update_plan", "create_plan", "plan", "revise_plan"}:
        return "planning", "Update plan", "tool identity"
    if name in {"edit_file", "write_file", "apply_patch", "create_file", "delete_file"} or call.get("family") == "file_write":
        return "implementation", "Edit file", "tool identity"
    if name in {"read_file", "list_directory", "list_files", "search_files", "grep", "web_search", "web_fetch", "kb_search"} or call.get("family") in {
        "file_read",
        "search",
    }:
        return "research", "Read file" if name == "read_file" else "Search / inspect", "tool identity"
    if name.startswith("ext_") and call.get("target_key") == "query":
        return "research", "External search", "external tool query parameter"
    if name in {"shell_execute", "run_command", "execute_command"}:
        from loom.evaluation.goal_loops.action_classification import inspection_command

        command = (call.get("target") or "").strip()
        if re.match(
            r"^(?:npx\s+tsc\b|(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:test|build|lint|typecheck)\b|(?:\S*/)?pytest\b|python(?:3)?\s+-m\s+pytest\b|cargo\s+(?:test|check)\b|go\s+test\b)",
            command,
        ):
            return "verification", "Build / test / type check", "recognized command prefix"
        if inspection_command(command):
            return "research", "Inspect workspace", "recognized inspection command"
        return "other", "Run command", "command purpose not established"
    return "other", call.get("tool") or "Tool call", "unclassified tool"


def action_flow(calls, rounds):
    by_id = {r["id"]: r for r in calls}
    actions, segments = [], []
    invoked = {r["model_call_id"]: [by_id[i] for i in r["tool_call_ids"]] for r in rounds if r.get("model_call_id")}
    for ordinal, call in enumerate(sorted(calls, key=lambda c: c["start_line"]), 1):
        tools = invoked.get(call["id"], [])
        subjects = tools or [call]
        classifications = [t.get("classification") or dict(zip(("kind", "title", "basis"), classify(t), strict=True)) for t in subjects]
        classes = [(c["kind"], c["title"], c["basis"]) for c in classifications]
        kinds = {c[0] for c in classes}
        kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
        if call["kind"] == "model":
            preparing = " + ".join(dict.fromkeys(t["tool"] for t in tools))
            title = "Working · " + (f"Preparing {preparing}" if preparing else classes[0][1])
            target = call.get("model") or ""
        else:
            title = call.get("tool") or classes[0][1]
            target = call.get("target") or ""
        start, end = timestamp(call.get("started_at")), timestamp(call.get("ended_at"))
        action = dict(
            id=call["id"],
            ordinal=ordinal,
            kind=kind,
            activity=call["kind"],
            title=title,
            target=target,
            status=call["status"],
            basis=list(dict.fromkeys(c[2] for c in classes)),
            classification_inferred=any(c.get("inferred", False) for c in classifications),
            classification_evidence=[r for c in classifications for r in c.get("evidence_refs", [])],
            classification_reasons=list(dict.fromkeys(c["rationale"] for c in classifications if c.get("rationale"))),
            call_ids=[call["id"]],
            start_line=call["start_line"],
            started_at=call.get("started_at"),
            ended_at=call.get("ended_at"),
            duration_ms=call.get("duration_ms"),
            duration_basis=call.get("duration_basis"),
            elapsed_ms=0,
            shared_ms=0,
            missing_intervals=int(start is None or end is None or end < start),
        )
        if not segments or segments[-1]["kind"] != kind:
            segments.append(dict(id="stage:" + action["id"], kind=kind, label=LABELS[kind], action_ids=[], elapsed_ms=0, shared_ms=0))
        action["segment_id"] = segments[-1]["id"]
        segments[-1]["action_ids"].append(action["id"])
        actions.append(action)
    action_by_id = {a["id"]: a for a in actions}
    owner = {cid: a["id"] for a in actions for cid in a["call_ids"]}
    changes = defaultdict(list)
    for call in calls:
        start, end = timestamp(call.get("started_at")), timestamp(call.get("ended_at"))
        if start is not None and end is not None and end >= start:
            changes[start].append((call["id"], 1))
            changes[end].append((call["id"], -1))
    gaps = []
    shared_segments = defaultdict(int)
    active, last, gap, parallel = Counter(), None, 0, 0
    for at, edges in sorted(changes.items()):
        if last is not None:
            duration = at - last
            live = [by_id[c] for c, n in active.items() if n > 0]
            holders = {owner[c["id"]] for c in live}
            models = {owner[c["id"]] for c in live if c["kind"] == "model"}
            # A recorded model interval nested inside another action's tool owns
            # that time. Concurrent model actions remain explicitly shared.
            nested = bool(models) and all(
                timestamp(tool["started_at"]) <= timestamp(model["started_at"]) and timestamp(tool["ended_at"]) >= timestamp(model["ended_at"])
                for tool in live
                if tool["kind"] == "tool"
                for model in live
                if model["kind"] == "model" and owner[tool["id"]] != owner[model["id"]]
            )
            selected = models if nested else holders
            if not holders:
                gap += duration
                if duration > 0:
                    gaps.append(
                        dict(
                            id=f"gap:{len(gaps) + 1}",
                            started_ms=last,
                            ended_ms=at,
                            duration_ms=duration,
                            next_action_ids=[owner[cid] for cid, change in edges if change > 0],
                        )
                    )
            elif len(selected) == 1:
                action_by_id[next(iter(selected))]["elapsed_ms"] += duration
            else:
                parallel += duration
                for aid in holders:
                    action_by_id[aid]["shared_ms"] += duration
                for sid in {action_by_id[aid]["segment_id"] for aid in holders}:
                    shared_segments[sid] += duration
        for cid, change in edges:
            active[cid] += change
        last = at
    for segment in segments:
        members = [action_by_id[i] for i in segment["action_ids"]]
        if all(a["title"] == "Working · Acceptance verification" for a in members):
            segment["label"] = "Acceptance verification"
        segment.update(
            elapsed_ms=sum(a["elapsed_ms"] for a in members),
            model_ms=sum(a["elapsed_ms"] for a in members if a["activity"] == "model"),
            tool_ms=sum(a["elapsed_ms"] for a in members if a["activity"] == "tool"),
            model_calls=sum(a["activity"] == "model" for a in members),
            tool_calls=sum(a["activity"] == "tool" for a in members),
            shared_ms=shared_segments[segment["id"]],
            failed_actions=sum(a["status"] == "failed" for a in members),
            missing_intervals=sum(a["missing_intervals"] for a in members),
            inferred_actions=sum(a["classification_inferred"] for a in members),
        )
    wall = max(changes) - min(changes) if changes else None
    assert wall is None or isclose(sum(s["elapsed_ms"] for s in segments) + gap + parallel, wall, abs_tol=0.001)
    return dict(
        version="action-flow.3",
        gaps=gaps,
        actions=actions,
        segments=segments,
        gap_ms=gap,
        parallel_ms=parallel,
        wall_span_ms=wall,
        missing_intervals=sum(a["missing_intervals"] for a in actions),
        timing_basis="timestamp partition; nested model time counted under its model action; simultaneous model actions shared",
        classification_basis="recorded operations, then cited saved assessments or declared action intent; model purpose follows recorded invocation links",
    )
