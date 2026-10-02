"""Recorded verification observations, with no automatic criterion acceptance."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import asdict

from loom.core import thaw_json
from loom.evaluation.evidence_store import EvidenceStore
from loom.trace_analysis.links import tool_output
from loom.trace_analysis.schemas import EpisodeGraph


def _assertions(store: EvidenceStore, event, path: str, content: str) -> list[dict]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []
    found = []
    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for node in ast.walk(tree):
        assertion = isinstance(node, ast.Assert)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assertion = node.func.attr.startswith("assert")
        if not assertion:
            continue
        # AST columns are byte offsets; evidence ranges are Unicode characters.
        start = offsets[node.lineno - 1] + len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
        end = offsets[node.end_lineno - 1] + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
        found.append({"text": content[start:end], "ref": asdict(store.ref(event, path, start=start, end=end)),
                      "execution_status": "unknown", "basis": "recorded_python_syntax"})
    return sorted(found, key=lambda row: row["ref"]["start"])


def build_verification_evidence(store: EvidenceStore, graph: EpisodeGraph, tool_rounds: dict) -> tuple[dict, ...]:
    rows, writes = [], []
    for tool in graph.tool_calls:
        event = tool.completed_event or tool.failed_event or tool.started_event
        if event is None:
            continue
        output, output_path = tool_output(event)
        output = thaw_json(output)
        result = output if isinstance(output, Mapping) else {}
        input_event = tool.started_event if tool.started_event and "input" in tool.started_event.payload else event
        arguments = input_event.payload.get("input", {})
        arguments = arguments if isinstance(arguments, Mapping) else {}
        refs = [asdict(store.ref(event, output_path))]
        if "input" in input_event.payload:
            refs.append(asdict(store.ref(input_event, "input")))
        base = {"id": f"verification:{tool.id}", "run_id": tool.run_id, "loop_id": tool.loop_id,
                "round_id": tool_rounds.get(tool.id), "tool_id": tool.tool_id, "tool_call_id": tool.tool_call_id,
                "evidence_refs": refs, "criterion_status": "unverified", "execution_status": "unknown",
                "scope": "Only this recorded observation", "line_number": event.line_number}
        is_command = "command" in arguments or "exit_code" in result or "stdout" in result or "shell" in tool.tool_id.lower()
        if is_command:
            exit_code = result.get("exit_code")
            exit_code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
            timed_out = result.get("timed_out")
            status = "unknown"
            if tool.failed_event is not None or timed_out is True or (exit_code is not None and exit_code != 0):
                status = "failed"
            elif exit_code == 0 and tool.completed_event is not None:
                status = "passed"
            warnings = []
            outputs = {}
            summaries = []
            for name in ("stdout", "stderr"):
                content = result.get(name)
                if isinstance(content, str) and output_path:
                    ref = asdict(store.ref(event, f"{output_path}.{name}"))
                    outputs[name] = {"ref": ref, "excerpt": content[:600], "char_length": len(content),
                                     "excerpt_truncated": len(content) > 600}
                    warnings.extend({"text": line[:500], "ref": ref} for line in content.splitlines() if "warning" in line.lower())
                    summaries.extend({"text": line[:500], "ref": ref} for line in content.splitlines()
                                     if re.search(r"\b\d+\s+(?:passed|failed|skipped|errors?)\b", line))
            rows.append({**base, "kind": "command_execution", "command": thaw_json(arguments.get("command", result.get("command"))),
                         "cwd": result.get("cwd", arguments.get("cwd")), "exit_code": exit_code,
                         "timed_out": timed_out, "execution_status": status, "outputs": outputs,
                         "warnings": warnings, "test_summary_observations": summaries,
                         "scope": "Exit status and output of the recorded command; criterion coverage requires separate analysis.",
                         "oracle_limitations": ["Printed success is not an assertion or task acceptance.",
                                                "Test output alone does not establish test implementation or feature coverage.",
                                                "No immutable binding between tested files and final artifact is established."],
                         "freshness": {"artifact_binding": "unknown", "later_recorded_writes": []}})
        content = result.get("content")
        if isinstance(content, str) and output_path:
            path = f"{output_path}.content"
            assertions = _assertions(store, event, path, content)
            rows.append({**base, "id": base["id"] + ":source", "kind": "recorded_assertions" if assertions else "recorded_content",
                         "path": result.get("path", arguments.get("path")), "content_ref": asdict(store.ref(event, path)),
                         "content_char_length": len(content), "truncated": result.get("truncated"), "assertions": assertions,
                         "scope": "Recorded file content, not evidence that these statements executed.",
                         "oracle_limitations": ["Assertion presence does not prove execution or adequacy.",
                                                "Snapshot-to-execution identity is unknown."]})
        if re.search(r"(?:^|[_.-])(write|edit|patch|delete|remove)(?:[_.-]|$)", tool.tool_id.lower()):
            write = {**base, "kind": "artifact_write", "path": arguments.get("path", result.get("path")),
                     "write_status": "recorded_completed" if tool.completed_event else "failed" if tool.failed_event else "attempted",
                     "scope": "Recorded tool write operation; resulting content requires its own evidence.",
                     "oracle_limitations": ["A completed write does not establish task correctness."]}
            writes.append(write)
            rows.append(write)
            written_content = arguments.get("content")
            if isinstance(written_content, str):
                assertions = _assertions(store, input_event, "input.content", written_content)
                rows.append({**base, "id": base["id"] + ":written-source",
                             "kind": "recorded_assertions" if assertions else "recorded_content",
                             "path": arguments.get("path"), "content_ref": asdict(store.ref(input_event, "input.content")),
                             "content_char_length": len(written_content), "assertions": assertions, "truncated": False,
                             "scope": "Requested write content; execution and resulting file identity remain separate.",
                             "oracle_limitations": ["Recorded write input does not establish that assertions executed."]})
    for row in rows:
        if row["kind"] == "command_execution":
            row["freshness"]["later_recorded_writes"] = [
                {"id": w["id"], "path": w["path"], "write_status": w["write_status"], "evidence_refs": w["evidence_refs"]}
                for w in writes if w["run_id"] == row["run_id"] and w["line_number"] > row["line_number"]
            ]
    for event in store.events:
        if event.event_type in {"run.completed", "step.completed", "trace.completed"}:
            rows.append({"id": f"runtime:{event.line_number}", "kind": "runtime_outcome", "run_id": event.run_id,
                         "loop_id": event.loop_id, "round_id": None, "line_number": event.line_number,
                         "execution_status": "unknown", "criterion_status": "unverified",
                         "reported_outcome": thaw_json(event.payload.get("outcome", event.payload.get("status"))),
                         "evidence_refs": [asdict(store.ref(event))],
                         "scope": "Runtime completion only; budget exhaustion may end a run.",
                         "oracle_limitations": ["Runtime pass is not criterion verification."]})
        elif event.event_type.startswith(("criterion.", "verification.")):
            rows.append({"id": f"criterion-event:{event.line_number}", "kind": "criterion_observation",
                         "run_id": event.run_id, "loop_id": event.loop_id, "round_id": None,
                         "line_number": event.line_number, "execution_status": "unknown", "criterion_status": "unverified",
                         "reported_status": event.payload.get("status"), "criterion_id": event.payload.get("criterion_id"),
                         "evidence_refs": [asdict(store.ref(event))], "scope": "Recorded evaluator claim",
                         "oracle_limitations": ["Evaluator implementation and execution require separate evidence."]})
    return tuple(sorted(rows, key=lambda row: row["line_number"]))
