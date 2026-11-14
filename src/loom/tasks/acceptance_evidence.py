"""Durable tool-call facts and criterion-directed evidence for acceptance.

The checkpoint retains a compact index and input previews; full outputs live in artifacts.
Only host-observed results enter this index, never solver claims.
"""

import json
from dataclasses import replace

from loom.core import Observation, ok, thaw_json
from loom.llm.context_budget import estimate_message_tokens
from loom.service.contracts import ServiceError


def record_call(assembly, run_id, call_id, tool, value, result):
    if assembly.acceptance.verifying:
        return result
    if result.ok and isinstance(result.value, Observation) and (result.value.metadata or {}).get("review_receipt"):
        from loom.tasks.context_review import replay

        return ok(replay(assembly, result.value))
    output = thaw_json(result.value.value) if result.ok and isinstance(result.value, Observation) else None
    error = None if result.ok else {"code": result.error.code, "message": result.error.message}
    # A transport may report an application error inside a successful observation.
    payload = output.get("result", output) if isinstance(output, dict) else output
    success = result.ok and isinstance(result.value, Observation) and not (isinstance(payload, dict) and payload.get("isError") is True)
    if isinstance(payload, dict) and (payload.get("timed_out") or payload.get("cancelled")
                                     or ("exit_code" in payload and payload["exit_code"] != 0)):
        success = False
    identifier = f"tool:{run_id}:{call_id}"
    artifact = output.get("artifact") if isinstance(output, dict) else None
    artifact_format = "output"
    if success and not artifact:
        # Reuse the receipt stored in the operation journal after a crash/replay.
        metadata = thaw_json(result.value.metadata or {})
        artifact = metadata.get("acceptance_artifact")
        if not artifact:
            artifact = assembly.publish_artifact({"input": value, "output": output}, "acceptance_tool_evidence")
            result = ok(replace(result.value, metadata={**metadata, "acceptance_artifact": artifact}))
        artifact_format = "call"
    inputs = json.dumps(value, ensure_ascii=False)
    binding = assembly.bindings[tool]
    row = {"id": identifier, "tool": tool, "ok": success, "goal_digest": assembly.acceptance.data["goal_digest"],
           "artifact": artifact, "artifact_format": artifact_format, "input": inputs[:2000], "input_truncated": len(inputs) > 2000,
           "error": error, "external": bool((binding.ref.metadata or {}).get("external_tool_id")),
           "paths": [v for k, v in value.items() if k in {"path", "file_path", "cwd"} and isinstance(v, str)]}
    if success and tool == "read_file" and row["paths"]:
        from loom.tasks.workspace_probe import ManifestUnavailable, manifest

        try:
            row["dependencies"] = manifest(assembly.tool_request.workspace, row["paths"])
        except (ValueError, OSError, ManifestUnavailable):
            row["dependencies"] = None
        # Bind to the version actually returned, even if the file changed
        # between acquisition and the validity probe.
        source_hash = (output.get("resource") or {}).get("after_sha256")
        if source_hash and row["dependencies"] is not None:
            row["dependencies"] = {row["paths"][0]: source_hash}
    # Journal replay must not count the same invocation twice.
    assembly._acceptance_calls = [r for r in assembly._acceptance_calls if r["id"] != identifier] + [row]
    if success:
        from loom.tasks.context_review import observe

        result = ok(observe(assembly, row, result.value))
    return result


def current_calls(assembly):
    return [r for r in assembly._acceptance_calls if r["goal_digest"] == assembly.acceptance.data["goal_digest"]]


def matching_calls(assembly, tool):
    return [r for r in current_calls(assembly) if r["tool"] == tool]


def binding(assembly):
    """Content and current source validity, independent of run/call identities."""
    from loom.tasks.workspace_probe import ManifestUnavailable, digest, manifest

    facts = set()
    excluded = {"finish", "update_plan", "plan", "complete_node", "revise_workflow", "revise_acceptance_plan"}
    for row in current_calls(assembly):
        if row["tool"] in excluded:
            continue
        current = None
        if row.get("dependencies") is not None:
            try:
                current = manifest(assembly.tool_request.workspace, row["paths"])
            except (ValueError, OSError, ManifestUnavailable):
                current = "unavailable"
        facts.add(digest({"tool": row["tool"], "input": row["input"], "ok": row["ok"],
                          "content": (row.get("artifact") or {}).get("sha256"), "current": current}))
    required_counts = {c["id"]: min(c["check"].get("min_calls", 1), sum(r["ok"] for r in matching_calls(assembly, c["check"]["tool_id"])))
                       for c in (assembly.acceptance.data.get("plan") or {}).get("criteria", []) if c["verifier"] == "tool_call"}
    return digest({"facts": sorted(facts), "required_counts": required_counts})


def coalesce_reads(rows):
    """Share identical local read content without losing invocation provenance.

    Execution counts still come from the journal. Commands and external reads
    remain separate: identical output does not imply the same operation/state.
    Different file versions, partial reads and errors must never be merged.
    """
    from loom.tasks.workspace_probe import digest

    result, seen = [], {}
    for row in rows:
        ref = row.get('artifact') or {}
        if row['tool'] not in {'read_file', 'list_directory'} or not row['ok'] or not ref.get('sha256') or row.get('external'):
            result.append(row)
            continue
        key = digest({k: row.get(k) for k in ('tool', 'input', 'input_truncated', 'artifact', 'artifact_format', 'dependencies', 'paths')})
        if key in seen:
            prior = seen[key]
            prior.setdefault('equivalent_call_ids', [prior['id']]).append(row['id'])
        else:
            seen[key] = dict(row)
            result.append(seen[key])
    return result


def evidence(assembly, *, targets=(), target_tools=(), target_sources=(), expanded=False, criteria=None, full=False, read_artifact=None):
    rows = current_calls(assembly)
    if not rows:
        # Legacy checkpoints only contain a rolling preview, not a complete call index.
        return assembly._acceptance_evidence
    criteria = (assembly.acceptance.data.get("plan") or {}).get("criteria", []) if criteria is None else criteria
    selected = {}

    def include(row):
        selected[row["id"]] = row

    # Notes guide selection only; verification still sees the original receipts
    # and independently checks the complete goal, source freshness and content.
    from loom.tasks.context_review import sync_goal

    review = sync_goal(assembly)
    if review is not None:
        cited = review.selected_evidence(criteria)
        for row in rows:
            if row["id"] in cited:
                include(row)

    # A structured gap can select already-acquired evidence that the default
    # view omitted. This changes the view, never re-executes the acquisition.
    for target in targets:
        for row in reversed(rows):
            if any(path == target or path.startswith(target.rstrip("/") + "/") for path in row.get("paths", [])):
                include(row)
    for row in reversed(rows):
        if row['tool'] in target_tools or (target_sources and (
                any(source in row.get('input', '') for source in target_sources) or row.get('external') or row['tool'] == 'fetch_url')):
            # External source URLs may only appear inside the output artifact,
            # so retain source-producing calls conservatively for source gaps.
            include(row)

    # Pin research/explicit-tool evidence before filling with recent unrelated work.
    scoped_review = full and bool(criteria) and all(c.get('scope') or c.get('required_tools') or c['verifier'] in {'tool_call', 'command'} for c in criteria)
    tools = set() if scoped_review else {r["tool"] for r in rows if r.get("external")}
    # A command check supplies its own fresh execution receipt and full output.
    # Do not attach every historical shell invocation merely because the same
    # executor is required. Explicit gap targets above can still request them.
    tools.update(t for c in criteria for t in c.get('required_tools', [])
                 if c['verifier'] != 'command' or t != c['check'].get('tool_id'))
    tools.update(c["check"]["tool_id"] for c in criteria if c["verifier"] == "tool_call")
    text = json.dumps([c for c in criteria if c['verifier'] != 'command'], ensure_ascii=False)
    tools.update(r["tool"] for r in rows if r["tool"] in text)
    summaries = []
    for tool in sorted(tools):
        matches = [r for r in rows if r["tool"] == tool]
        successful = [r for r in matches if r["ok"]]
        # First and last alone can omit entire research subjects in the middle.
        # Keep up to 16 evenly distributed successful observations per required tool.
        pool = successful or matches
        if pool:
            count = len(pool) if full else min(16, len(pool))
            for i in range(count):
                include(pool[i * (len(pool) - 1) // max(1, count - 1)])
        summaries.append({"tool": tool, "calls": len(matches), "successful": len(successful)})
    scopes = {p for c in criteria for p in c.get("scope", [])}
    scoped = {}
    for row in rows:
        for path in row.get("paths", []):
            if any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in scopes):
                scoped[(row["tool"], path)] = row
    # Keep the latest observation for every referenced path within the shared
    # evidence cap. A recent-only slice drops early configuration files from
    # broad architecture reviews even when the verifier has ample input budget.
    for row in scoped.values():
        include(row)
    if not scoped_review:
        for row in (rows if full else rows[-8:]):
            include(row)
    # Bound prompt size while making sampling/truncation explicit to the judge.
    selected_rows = coalesce_reads(selected.values())
    chosen = selected_rows if full else selected_rows[:48]
    result = [{"id": "tool-call-coverage", "total_calls": len(rows), "reviewed_calls": len(chosen),
               "omitted_calls": len(rows) - len(chosen), "tools": summaries,
               "note": "Call success proves execution only. Assess source relevance and deliverable correctness separately; "
                       "missing or truncated supporting content cannot establish a claim."}]
    result.extend(materialize_calls(assembly, chosen, full=full, expanded=expanded, read_artifact=read_artifact))
    # Leave half of the verification context for the goal, criteria, candidate and
    # deterministic checks. The full artifacts remain available outside this prompt.
    token_budget = assembly.acceptance.input_budget()["max_input_tokens"] // 2
    while not full and estimate_message_tokens("", json.dumps(result, ensure_ascii=False)) > token_budget and len(result) > 1:
        largest = max(result[1:], key=lambda r: len(r.get("output") or "") + len(r.get("input", "")))
        if len(largest.get("output") or "") > 512:
            largest["output"] = largest["output"][:len(largest["output"]) // 2]
            largest["truncated"] = True
        elif len(largest.get("input", "")) > 256:
            largest["input"] = largest["input"][:len(largest["input"]) // 2]
            largest["input_truncated"] = True
        else:
            result.pop()
    represented = sum(len(row.get('equivalent_call_ids', [row['id']])) for row in result[1:])
    result[0].update(reviewed_calls=represented, omitted_calls=len(rows) - represented,
                     evidence_items=len(result) - 1, deduplicated_calls=represented - (len(result) - 1))
    return result


def materialize_calls(assembly, rows, *, full=False, expanded=False, read_artifact=None):
    """Load registered receipts with source freshness checks, independent of selection."""
    from loom.tasks.context_review import sync_goal

    review = sync_goal(assembly)
    result = []
    for row in rows:
        # Dependency fingerprints are host validity metadata, not model evidence.
        dependencies = row.get("dependencies")
        row = {k: v for k, v in row.items() if k != "dependencies"}
        if review is not None and row["id"] in review.data["sources"]:
            row["review_analysis"] = review.data["sources"][row["id"]]["state"]
        if not row.get("artifact"):
            result.append({**row, "output": None})
            continue
        try:
            stored = (read_artifact or assembly.read_artifact)(row["artifact"]["sha256"])
        except (OSError, ValueError, KeyError, ServiceError):
            result.append({**row, "unavailable": True})
            continue
        if dependencies is not None:
            from loom.tasks.workspace_probe import ManifestUnavailable, manifest

            try:
                fresh = manifest(assembly.tool_request.workspace, row["paths"]) == dependencies
            except (ValueError, OSError, ManifestUnavailable):
                fresh = False
            if not fresh:
                result.append({**row, "stale": True, "output": None})
                continue
        raw = json.dumps(stored["output"] if row["artifact_format"] == "call" else stored, ensure_ascii=False)
        cap = len(raw) if full else assembly.acceptance.input_budget()["max_input_tokens"] if expanded else 6000
        result.append({**row, "output": raw[:cap], "truncated": len(raw) > cap})
    return result
