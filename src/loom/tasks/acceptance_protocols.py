"""Acceptance wire contracts. Host-owned goal checks are added after validation."""

from loom.core import thaw_json
from loom.llm.structured import StructuredProtocol


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}


def normalize_plan(value):
    changes = []

    def default(row, key, val, path):
        if key not in row:
            row[key] = val
            changes.append({"path": f"{path}/{key}", "rule": "contract_default"})

    if not isinstance(value, dict):
        return value, changes
    default(value, "unresolved_requirements", [], "")
    criteria = value.get("criteria")
    if isinstance(criteria, list):
        for index, row in enumerate(criteria):
            if not isinstance(row, dict):
                continue
            path = f"/criteria/{index}"
            default(row, "scope", [], path)
            default(row, "check", {}, path)
            default(row, "required", True, path)
            if isinstance(row["scope"], str):
                row["scope"] = [row["scope"]]
                changes.append({"path": path + "/scope", "rule": "single_path_to_array"})
            if isinstance(row["check"], dict):
                if row.get("verifier") == "tool_call":
                    default(row["check"], "min_calls", 1, path + "/check")
                if row.get("verifier") == "artifact":
                    default(row["check"], "nonempty", True, path + "/check")
                    default(row["check"], "contains", [], path + "/check")
    requirements = value.get("unresolved_requirements")
    if isinstance(requirements, list):
        for i, row in enumerate(requirements):
            if isinstance(row, dict) and set(row) <= {"id", "description"}:
                description, identifier = row.get("description"), row.get("id")
                if isinstance(description, str) and description.strip() and (identifier is None or isinstance(identifier, str)):
                    requirements[i] = f"{identifier}: {description}" if identifier else description
                    changes.append({"path": f"/unresolved_requirements/{i}", "rule": "requirement_description"})
    return value, changes


def plan_protocol(assembly):
    variants = []

    def criterion(kind, check):
        variants.append(obj({"id": STRING, "description": STRING, "verifier": {"type": "string", "enum": [kind]},
                             "scope": STRINGS, "required": {"type": "boolean", "enum": [True]}, "check": check}))

    criterion("semantic", obj({}))
    if "browser_capture" in assembly.bindings:
        from loom.browser.scenarios import SCENARIO_SCHEMA

        criterion("visual", obj({"scenario": SCENARIO_SCHEMA}))
    criterion("artifact", obj({"path": STRING, "nonempty": {"type": "boolean", "enum": [True]}, "contains": STRINGS}))
    criterion("source", obj({"urls": STRINGS}))
    if assembly.bindings:
        criterion("tool_call", obj({"tool_id": {"type": "string", "enum": list(assembly.bindings)}, "min_calls": {"type": "integer"}}))
    for tool in ("shell_execute", "process_execute"):
        if tool in assembly.bindings:
            # Use the actual registered input contract. Native schema compilation
            # handles provider strictness separately from the local contract.
            check = obj({"tool_id": {"type": "string", "enum": [tool]}, "input": thaw_json(assembly.bindings[tool].ref.input_schema)})
            criterion("command", check)
    def normalize(value):
        value, changes = normalize_plan(value)
        if not isinstance(value, dict) or not isinstance(value.get("criteria"), list):
            return value, changes
        for i, row in enumerate(value["criteria"]):
            if isinstance(row, dict) and row.get("verifier") == "visual" and isinstance(row.get("check"), dict) and "scenario" in row["check"]:
                from loom.browser.scenarios import normalize_scenario

                row["check"]["scenario"], edits = normalize_scenario(row["check"]["scenario"], path=f"/criteria/{i}/check/scenario")
                changes.extend(edits)
            if not isinstance(row, dict) or row.get("verifier") != "command" or not isinstance(row.get("check"), dict):
                continue
            check = row["check"]
            tool = check.get("tool_id")
            if not isinstance(tool, str) or tool not in assembly.bindings or not isinstance(check.get("input"), dict):
                continue
            schema = thaw_json(assembly.bindings[tool].ref.input_schema)
            for key, definition in schema.get("properties", {}).items():
                if (key in check["input"] and check["input"][key] is None and key not in schema.get("required", [])
                        and isinstance(definition.get("type"), str)
                        and definition.get("type") in {"string", "number", "integer", "boolean", "array", "object"}):
                    del check["input"][key]
                    changes.append({"path": f"/criteria/{i}/check/input/{key}", "rule": "native_optional_null"})
        return value, changes

    return StructuredProtocol("acceptance_plan", 1,
                              obj({"criteria": {"type": "array", "items": {"anyOf": variants}}, "unresolved_requirements": STRINGS}),
                              normalize)


def verification_protocol(criteria, evidence):
    row = obj({"criterion_id": {"type": "string", "enum": [c["id"] for c in criteria]},
               "status": {"type": "string", "enum": ["passed", "failed", "blocked"]}, "reason": STRING,
               "evidence_ids": {"type": "array", "items": {"type": "string", "enum": [e["id"] for e in evidence]}}})
    return StructuredProtocol("acceptance_verification", 1, obj({"results": {"type": "array", "items": row}}))


def validate_verification(value, criteria, evidence):
    rows = value.get("results")
    expected = {c["id"] for c in criteria}
    if not isinstance(rows, list) or len(rows) != len(expected) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("Verifier must return one result per semantic criterion")
    if any(not isinstance(r.get("criterion_id"), str) for r in rows) or {r["criterion_id"] for r in rows} != expected:
        raise ValueError("Verifier returned missing, duplicate or unknown criteria")
    available = {e["id"] for e in evidence}
    for row in rows:
        if row.get("status") not in {"passed", "failed", "blocked"} or not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Invalid semantic verification result")
        refs = row.get("evidence_ids", [])
        if (not isinstance(refs, list) or any(not isinstance(r, str) or r not in available for r in refs)
                or (row["status"] == "passed" and not refs)):
            raise ValueError("Semantic verification requires registered evidence")
    return rows
