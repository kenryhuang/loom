"""Validate and commit independent memory records, retaining accepted siblings.

Array admission uses the exact request contract; item validation is independent
so one bad citation cannot discard other supported records. Only declared rule
violations become model feedback. Programming errors propagate to the service.
"""

from copy import deepcopy

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.packing import serialized
from loom.evaluation.issues import RuleViolation, issue, protocol_issue, validate_schema
from loom.evaluation.memory.contracts import FIELDS, PROTOCOL, protocol_for
from loom.evaluation.memory.evidence import sanitize
from loom.evaluation.memory.validation import validate_record
from loom.llm.api import LlmResponse
from loom.llm.structured import ProtocolError, parse_response


def commit_records(memory, unit, response, *, schema=None, finish_reason=None):
    errors = []
    unit.update(issues=errors, rejected_records=[], omitted_rejected_records=0)

    def reject(error, kind=None, index=None, row=None):
        error = {**error, "stage": "memory", "unit_id": unit["id"]}
        if kind is not None:
            error["record_type"] = kind
        if index is not None:
            error["record_index"] = index + 1
            rejected = dict(kind=kind, index=index + 1, record=sanitize(row), error=error["reason"], issue=error)
            if len(serialized(unit["rejected_records"] + [rejected])) <= 16000:
                unit["rejected_records"].append(rejected)
            else:
                unit["omitted_rejected_records"] += 1
        errors.append(error)

    try:
        value, _ = parse_response(LlmResponse(content=response, finish_reason=finish_reason), PROTOCOL.max_response_chars)
    except ProtocolError as exc:
        reject(protocol_issue(exc))
        return errors
    if not isinstance(value, dict):
        reject(issue("response.object", "Return a memory object with contributions, facts, lessons and workflows arrays", category="format", phase="decode"))
        return errors
    schema = schema if schema is not None else protocol_for(unit).schema
    if set(value) - set(FIELDS):
        reject(issue("response.fields", "Unknown memory fields; allowed: contributions, facts, lessons, workflows", category="format", phase="decode"))
    accepted_stages = {r["stage_id"] for r in memory["records"].values() if r["unit_id"] == unit["id"] and r["kind"] == "contributions"}
    for kind in FIELDS:
        rows = value.get(kind)
        try:
            # Enforce mode and pending-stage capacity from the request, without
            # letting a bad item roll back valid siblings in the same array.
            spec = {k: v for k, v in schema["properties"][kind].items() if k != "items"}
            validate_schema(rows, spec)
        except RuleViolation as exc:
            reject(exc.issue, kind)
            continue
        for index, row in enumerate(rows):
            try:
                record = validate_record(kind, row, unit)
            except RuleViolation as exc:
                reject(exc.issue, kind, index, row)
                continue
            if kind == "contributions":
                key = identity(kind, unit["id"], row["stage_id"])
            elif kind == "workflows":
                key = identity(kind, unit["id"], sorted(row["goal_ids"]))
            else:
                key = identity(kind, {k: record[k] for k in FIELDS[kind]["items"]["properties"]})
            record.update(id="memory:" + key, kind=kind, unit_id=unit["id"], **{
                k: memory["identity"][source] for k, source in (("source_digest", "source"), ("run_id", "run_id"), ("graph_revision", "graph_revision"))
            })
            memory["records"][record["id"]] = record
            if kind == "contributions":
                accepted_stages.add(row["stage_id"])
    if unit["mode"] == "extract":
        missing = sorted({s["id"] for s in unit["scope"]["stages"]} - accepted_stages)
        if missing:
            reject(issue("memory.stages_pending", "Rate every delivered stage, using unknown when needed: " + ", ".join(missing),
                         category="coverage", phase="coverage", stage_ids=missing), "contributions")
    unit["accepted_records"] = [
        {k: r[k] for k in ("kind", "title", "summary", "stage_id", "rating") if k in r}
        for r in memory["records"].values() if r["unit_id"] == unit["id"]
    ][:48]
    unit["issues"] = deepcopy(errors)
    return errors


def commit(memory, unit, response):
    """Legacy string-returning API. Runtime and policy use commit_records."""
    return [f"{e.get('record_type', 'response')}: "
            + (f"record {e['record_index']}: " if 'record_index' in e else "") + e["reason"]
            for e in commit_records(memory, unit, response)]
