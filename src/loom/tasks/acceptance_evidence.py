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
    # Journal replay must not count the same invocation twice.
    assembly._acceptance_calls = [r for r in assembly._acceptance_calls if r["id"] != identifier] + [row]
    return result


def current_calls(assembly):
    return [r for r in assembly._acceptance_calls if r["goal_digest"] == assembly.acceptance.data["goal_digest"]]


def matching_calls(assembly, tool):
    return [r for r in current_calls(assembly) if r["tool"] == tool]


def evidence(assembly):
    rows = current_calls(assembly)
    if not rows:
        # Legacy checkpoints only contain a rolling preview, not a complete call index.
        return assembly._acceptance_evidence
    criteria = (assembly.acceptance.data.get("plan") or {}).get("criteria", [])
    selected = {}

    def include(row):
        selected[row["id"]] = row

    # Pin research/explicit-tool evidence before filling with recent unrelated work.
    tools = {r["tool"] for r in rows if r.get("external")}
    tools.update(c["check"]["tool_id"] for c in criteria if c["verifier"] == "tool_call")
    text = json.dumps(criteria, ensure_ascii=False)
    tools.update(r["tool"] for r in rows if r["tool"] in text)
    summaries = []
    for tool in sorted(tools):
        matches = [r for r in rows if r["tool"] == tool]
        successful = [r for r in matches if r["ok"]]
        # First and last alone can omit entire research subjects in the middle.
        # Keep up to 16 evenly distributed successful observations per required tool.
        pool = successful or matches
        if pool:
            for i in range(min(16, len(pool))):
                include(pool[i * (len(pool) - 1) // max(1, min(16, len(pool)) - 1)])
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
    for row in rows[-8:]:
        include(row)
    # Bound prompt size while making sampling/truncation explicit to the judge.
    chosen = list(selected.values())[:48]
    result = [{"id": "tool-call-coverage", "total_calls": len(rows), "reviewed_calls": len(chosen),
               "omitted_calls": len(rows) - len(chosen), "tools": summaries,
               "note": "Call success proves execution only. Assess source relevance and deliverable correctness separately; "
                       "missing or truncated supporting content cannot establish a claim."}]
    for row in chosen:
        if not row.get("artifact"):
            result.append({**row, "output": None})
            continue
        try:
            full = assembly.read_artifact(row["artifact"]["sha256"])
        except (OSError, ValueError, KeyError, ServiceError):
            result.append({**row, "unavailable": True})
            continue
        raw = json.dumps(full["output"] if row["artifact_format"] == "call" else full, ensure_ascii=False)
        result.append({**row, "output": raw[:6000], "truncated": len(raw) > 6000})
    # Leave half of the verification context for the goal, criteria, candidate and
    # deterministic checks. The full artifacts remain available outside this prompt.
    token_budget = assembly.acceptance.input_budget()["max_input_tokens"] // 2
    while estimate_message_tokens("", json.dumps(result, ensure_ascii=False)) > token_budget and len(result) > 1:
        largest = max(result[1:], key=lambda r: len(r.get("output") or "") + len(r.get("input", "")))
        if len(largest.get("output") or "") > 512:
            largest["output"] = largest["output"][:len(largest["output"]) // 2]
            largest["truncated"] = True
        elif len(largest.get("input", "")) > 256:
            largest["input"] = largest["input"][:len(largest["input"]) // 2]
            largest["input_truncated"] = True
        else:
            result.pop()
    result[0]["reviewed_calls"] = len(result) - 1
    result[0]["omitted_calls"] = len(rows) - (len(result) - 1)
    return result
