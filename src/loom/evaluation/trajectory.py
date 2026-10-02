"""Build evidence-grounded factual ledgers from the registered trace snapshot."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict

from loom.core import thaw_json
from loom.evaluation.context_analysis import build_context_deltas, observed_injections, request_fields
from loom.evaluation.diagnostics import FactAnalysis
from loom.evaluation.evidence_store import EvidenceStore, text_value
from loom.evaluation.token_ledger import build_token_ledger
from loom.evaluation.verification import build_verification_evidence
from loom.trace_analysis.links import linked_tools_for_round, tool_output
from loom.trace_analysis.schemas import EpisodeGraph


def _parse_task(text: str) -> tuple[str | None, list[dict]]:
    objective, criteria = None, []
    in_criteria = False
    for line in text.splitlines():
        clean = re.sub(r"^\s*(?:#{1,6}\s*|[-*]\s*)", "", line).strip()
        match = re.match(r"(?i)(?:objective|goal)\s*:\s*(.*)", clean)
        if match:
            if match[1].strip():
                objective = match[1].strip()
            in_criteria = False
            continue
        match = re.match(r"(?i)(?:success criteria|acceptance criteria)\s*:\s*(.*)", clean)
        if match:
            in_criteria = True
            if match[1].strip():
                criteria.append({"description": match[1].strip()})
            continue
        if in_criteria:
            if re.match(r"^[\w ]+:\s*$", clean) or line.lstrip().startswith("#"):
                in_criteria = False
            elif clean:
                criterion = re.match(r"([^:]+?)(?:\s*\((required|optional)\))?\s*:\s*(.+)", clean)
                if criterion:
                    criteria.append({"source_id": criterion[1].strip(), "description": criterion[3].strip(),
                                     "required": criterion[2] != "optional"})
                elif re.match(r"\s*(?:[-*]|\d+[.)])\s+", line):
                    criteria.append({"description": re.sub(r"^\d+[.)]\s*", "", clean)})
    return objective, criteria


def _task_contracts(store: EvidenceStore, graph: EpisodeGraph, task: str | None) -> tuple[dict, ...]:
    rows, last_by_origin, signatures = [], {}, set()
    run_ids = list(dict.fromkeys(r.run_id for r in graph.llm_rounds))
    run_ids.extend(r.run_id for r in graph.runs if r.run_id not in run_ids)
    if not run_ids:
        run_ids = list(dict.fromkeys(e.run_id for e in store.events)) or [None]

    def append(run_id, objective, criteria, origin, refs, *, round_id=None, source_text=None):
        signature = (run_id, origin, objective, json.dumps(criteria, sort_keys=True))
        key = run_id, origin
        # Only consecutive identical definitions are retained; reverting is a revision.
        if last_by_origin.get(key, (None, None))[1] == signature:
            return
        identifier = f"task:{len(rows) + 1}"
        normalized = [{**criterion, "id": f"{identifier}:criterion:{i + 1}", "status": "unverified",
                       "evidence_refs": refs} for i, criterion in enumerate(criteria)]
        if objective:
            normalized.append({"id": f"{identifier}:objective", "description": objective, "origin": "objective",
                               "required": True, "status": "unverified", "evidence_refs": refs})
        supersedes = last_by_origin.get(key, (None, None))[0]
        rows.append({"id": identifier, "run_id": run_id, "objective": objective, "criteria": normalized,
                     "origin": origin, "evidence_refs": refs, "source_refs": refs, "round_id": round_id,
                     "supersedes": supersedes, "source_text": source_text,
                     "coverage": "explicit_definition" if objective else "unknown"})
        last_by_origin[key] = identifier, signature
        signatures.add((run_id, origin))

    if task is not None:
        parsed, criteria = _parse_task(task)
        for run_id in run_ids:
            append(run_id, parsed or task, criteria, "explicit_task", [], source_text=task)
    round_for_line = {r.requested_event.line_number: r.id for r in graph.llm_rounds if r.requested_event}
    for event in store.events:
        definitions = [("metadata", event.payload.get("metadata")), ("task", event.payload.get("task")),
                       ("goal", event.payload.get("goal"))]
        if event.event_type.startswith(("task.", "goal.")):
            definitions.append((None, event.payload))
        for prefix, value in definitions:
            if not isinstance(value, Mapping):
                continue
            definition, path = value, prefix
            if isinstance(value.get("goal"), Mapping):
                definition, path = value["goal"], (prefix + "." if prefix else "") + "goal"
            elif isinstance(value.get("task"), Mapping):
                definition, path = value["task"], (prefix + "." if prefix else "") + "task"
            objective = definition.get("objective")
            if not isinstance(objective, str):
                continue
            criteria = []
            source_criteria = definition.get("success_criteria", definition.get("successCriteria", definition.get("criteria", [])))
            if isinstance(source_criteria, list | tuple):
                for criterion in source_criteria:
                    if isinstance(criterion, str):
                        criteria.append({"description": criterion})
                    elif isinstance(criterion, Mapping) and isinstance(criterion.get("description"), str):
                        criteria.append({"description": criterion["description"], "source_id": criterion.get("id"),
                                         "required": criterion.get("required")})
            append(event.run_id, objective, criteria, "trace_metadata", [asdict(store.ref(event, path))])
        if event.event_type != "llm.requested":
            continue
        messages, path, _, _ = request_fields(event)
        if not isinstance(messages, list | tuple):
            continue
        definitions = []
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping) or message.get("role") not in {"user", "system", "developer"}:
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            objective, criteria = _parse_task(content)
            if objective or criteria:
                definitions.append((index, objective, criteria, content))
        if not definitions and (event.run_id, "initial_request") not in signatures and (event.run_id, "request_goal") not in signatures:
            for index, message in enumerate(messages):
                if isinstance(message, Mapping) and message.get("role") == "user" and isinstance(message.get("content"), str):
                    content = message["content"]
                    append(event.run_id, content, [], "initial_request", [asdict(store.ref(event, f"{path}.{index}.content"))],
                           round_id=round_for_line.get(event.line_number))
                    break
        for index, objective, criteria, _ in definitions[-1:]:
            append(event.run_id, objective, criteria, "request_goal", [asdict(store.ref(event, f"{path}.{index}.content"))],
                   round_id=round_for_line.get(event.line_number))
    return tuple(rows)


def build_fact_analysis(store: EvidenceStore, graph: EpisodeGraph, task: str | None = None) -> FactAnalysis:
    trajectory, tool_uses, owners, bases = [], [], {}, {}
    for item in graph.llm_rounds:
        request, response = item.requested_event, item.completed_event
        events = [e for e in (request, response, item.failed_event) if e is not None]
        trajectory.append({"id": item.id, "run_id": item.run_id, "loop_id": item.loop_id,
                           "trace_id": item.trace_id, "step_number": item.step_number, "llm_call_id": item.llm_call_id,
                           "status": item.status, "request_ref": asdict(store.ref(request)) if request else None,
                           "response_ref": asdict(store.ref(response, "response" if "response" in response.payload else None)) if response else None,
                           "evidence_refs": [asdict(store.ref(e)) for e in events],
                           "response_claim_status": "model_output_not_independently_verified"})
        for tool, basis in linked_tools_for_round(graph, item):
            if tool.id not in owners:
                owners[tool.id], bases[tool.id] = item.id, basis
            elif owners[tool.id] != item.id:
                owners[tool.id], bases[tool.id] = None, "ambiguous"
    for tool in graph.tool_calls:
        event = tool.completed_event or tool.failed_event
        output, path = tool_output(event) if event else (None, None)
        input_event = tool.started_event or event
        input_ref = asdict(store.ref(input_event, "input")) if input_event and "input" in input_event.payload else None
        output_ref = asdict(store.ref(event, path)) if event and path else None
        text = text_value(output) if path else ""
        tool_uses.append({"id": tool.id, "round_id": owners.get(tool.id), "run_id": tool.run_id, "loop_id": tool.loop_id,
                          "trace_id": tool.trace_id, "step_number": tool.step_number, "tool_id": tool.tool_id,
                          "tool_call_id": tool.tool_call_id, "status": tool.status,
                          "link_basis": bases.get(tool.id, "unlinked"), "input_ref": input_ref,
                          "raw_output_ref": output_ref, "output_excerpt": text[:600], "output_char_length": len(text) if path else None,
                          "output_excerpt_truncated": len(text) > 600,
                          "source_truncated": output.get("truncated") if isinstance(output, Mapping) else None,
                          "injections": observed_injections(store, graph, tool, output),
                          "semantic_consumption": "unknown", "task_effect": "not_assessed",
                          "evidence_refs": [asdict(store.ref(e)) for e in (tool.started_event, tool.completed_event, tool.failed_event) if e]})
    ledger, tokens = build_token_ledger(store, graph)
    contexts = build_context_deltas(store, graph)
    return FactAnalysis(task_contracts=_task_contracts(store, graph, task), trajectory=tuple(trajectory),
                        context_deltas=contexts, tool_uses=tuple(tool_uses), token_ledger=ledger,
                        verification_evidence=build_verification_evidence(store, graph, owners),
                        coverage={"status": "recorded_facts_only", "source": thaw_json(store.coverage), "tokens": tokens,
                                  "round_count": len(trajectory), "unlinked_tool_count": sum(u["round_id"] is None for u in tool_uses),
                                  "missing_request_count": sum(r["request_ref"] is None for r in trajectory),
                                  "semantic_consumption": "not_assessed", "task_completion": "unverified"})
