"""Wire decoding only; no inferred values or semantic state mutation."""

from loom.evaluation.goal_loops.graph_contract import UPDATE_FIELDS
from loom.evaluation.issues import issue, protocol_issue
from loom.llm.api import LlmResponse
from loom.llm.structured import ProtocolError, parse_response


def parse_updates(answer):
    """Decode JSON or complete typed JSONL lines; never reconstruct a broken tail.

    Legacy group envelopes are expanded explicitly. Their records still commit
    independently. Wire failures have precise codes and never escape as TypeError.
    """
    records, errors = [], []
    if len(answer.content or "") > 1_000_000:
        return [], [issue("response.response_too_large", "Response exceeds 1000000 characters", category="format", phase="decode",
                          details=dict(limit=1_000_000, actual_count=len(answer.content)))]

    def reject(code, reason, **context):
        errors.append(issue(code, reason, category="format", phase="decode", **context))

    def unpack(value, *, depth=0, **location):
        if not isinstance(value, dict):
            reject("response.object", "Expected an update object", **location)
            return
        if "type" in value:
            kind = value["type"]
            if not isinstance(kind, str) or kind not in UPDATE_FIELDS:
                reject("response.record_type", "Unknown update type; use a declared record type", path=("type",), **location)
                return
            records.append((kind, {k: v for k, v in value.items() if k != "type"}))
            return
        if "groups" in value:
            groups = value["groups"]
            if depth or not isinstance(groups, list) or len(groups) > 8:
                reject("response.groups", "Expected at most eight non-nested legacy groups", path=("groups",), **location)
            else:
                for index, group in enumerate(groups):
                    unpack(group, depth=1, group_index=index + 1, **location)
        for kind, rows in value.items():
            if kind == "groups":
                continue
            if kind not in UPDATE_FIELDS:
                reject("response.fields", "Unknown update field", record_type=kind, **location)
                continue
            if not isinstance(rows, list):
                reject("schema.type", "Expected an update array", record_type=kind, **location)
                continue
            limit = UPDATE_FIELDS[kind]["maxItems"]
            if len(rows) > limit:
                reject("schema.maxItems", f"At most {limit} update records; received {len(rows)}", record_type=kind,
                       details=dict(limit=limit, actual_count=len(rows)), **location)
                continue
            records.extend((kind, row) for row in rows)

    try:
        value, _ = parse_response(answer)
    except ProtocolError as whole_error:
        lines = (answer.content or "").splitlines()
        if whole_error.code not in {"invalid_json", "truncated"} or len(lines) > 256:
            return [], [protocol_issue(whole_error)]
        recognized = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value, _ = parse_response(LlmResponse(content=line, finish_reason="stop"))
            except ProtocolError as exc:
                errors.append(protocol_issue(exc, response_line=index + 1))
                continue
            if not isinstance(value, dict) or "type" not in value:
                reject("response.json_line", "Expected a complete typed record on this line", response_line=index + 1)
                continue
            recognized = True
            unpack(value, response_line=index + 1)
        if not recognized:
            return [], [protocol_issue(whole_error)]
        if whole_error.code == "truncated":
            reject("response.truncated", "Response truncated; complete independent records retained")
    else:
        unpack(value)
    if len(records) > 128:
        return [], [issue("response.record_limit", "Too many update records", category="format", phase="decode", details=dict(limit=128))]
    return records, errors
