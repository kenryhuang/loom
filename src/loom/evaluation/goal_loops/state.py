"""Validate independent patch entries against delivered evidence, then commit a revision."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from loom.evaluation.goal_loops.contracts import PATCH, UPDATE_FIELDS
from loom.evaluation.goal_loops.facts import identity


def overlaps(a, b):
    if any(a.get(k) != b.get(k) for k in ("source_sha256", "event_hash", "line_number", "field_path")):
        return False
    return max(a.get("start") or 0, b.get("start") or 0) < min(
        a.get("end") if a.get("end") is not None else float("inf"), b.get("end") if b.get("end") is not None else float("inf")
    )


KINDS = ("goals", "attempts", "phases", "activities")


def root_of(identifier, objects):
    seen = set()
    while identifier in objects and identifier not in seen:
        seen.add(identifier)
        row = objects[identifier]
        parent = row.get("parent_goal_id") or row.get("goal_id") or row.get("attempt_id")
        if not parent:
            return identifier
        identifier = parent
    return None


def apply_patch(state, patch, delivered, atoms, stage):
    result = deepcopy(state)
    objects = {r["id"]: r for kind in KINDS for r in result[kind]}
    call_map = {a["id"]: a for a in atoms}
    names, issues, referenced = {}, [], set()

    def resolve(key):
        value = names.get(key, key)
        # Reconciliation aliases never erase historical identities.
        for _ in range(len(result["aliases"]) + 1):
            if value not in result["aliases"]:
                return value
            value = result["aliases"][value]
        raise ValueError("Alias cycle")

    def refs(keys):
        if not keys or any(k not in delivered or not delivered[k].get("evidence_ref") for k in keys):
            raise ValueError("Refs must name nonempty, actually delivered evidence packets")
        referenced.update(keys)
        return [delivered[k]["evidence_ref"] for k in dict.fromkeys(keys)]

    def calls(keys, evidence):
        if any(k not in call_map for k in keys):
            raise ValueError("Unknown or out-of-run call ID")
        lines = {r["line_number"] for r in evidence}
        if any(not lines.intersection(r["line_number"] for r in call_map[k]["evidence_refs"]) for k in keys):
            raise ValueError("Call attribution needs evidence from that call, not its surrounding interval")
        return list(dict.fromkeys(keys))

    def accept(kind, raw):
        key = raw["key"]
        if not key or key in names or key in objects:
            raise ValueError("New keys must be unique; use existing IDs only for references")
        identifier = ("activity" if kind == "activities" else kind[:-1]) + ":" + identity(stage, kind, key)
        evidence = refs(raw["refs"])
        row = {k: v for k, v in raw.items() if k not in {"key", "refs"}}
        row.update(
            id=identifier,
            evidence_refs=evidence,
            basis="inferred",
            confidence=raw.get("confidence", "low"),
            start_line=min(r["line_number"] for r in evidence),
            end_line=max(r["line_number"] for r in evidence),
        )
        parent_field = {"goals": "parent_id", "attempts": "goal_id", "phases": "attempt_id", "activities": "goal_id"}[kind]
        parent = resolve(raw[parent_field])
        expected = result["attempts"] if kind == "phases" else result["goals"]
        if parent not in {r["id"] for r in expected}:
            raise ValueError("Parent must be an existing goal/attempt or an earlier valid patch key")
        row.pop("parent_id", None)
        row["parent_goal_id" if kind == "goals" else parent_field] = parent
        root = objects.get(root_of(parent, objects))
        if root and any(not root["origin_line"] <= r["line_number"] <= root["end_line"] for r in evidence):
            raise ValueError("Entity crosses an effective goal revision; use explicit result reuse instead")
        if "call_ids" in row:
            row["call_ids"] = calls(row["call_ids"], evidence)
        if kind == "goals":
            row.update(goal_disposition="unknown", completion_claim="unknown")
        result[kind].append(row)
        objects[identifier] = row
        names[key] = identifier

    def guarded(label, raw, fn):
        prior_refs = set(referenced)
        try:
            fn(raw)
        except (ValueError, KeyError, TypeError) as exc:
            referenced.intersection_update(prior_refs)
            issues.append(dict(stage=stage, field=label, key=raw.get("key"), reason=str(exc)))

    for kind in KINDS:
        for raw in patch.get(kind, []):
            guarded(kind, raw, lambda raw, kind=kind: accept(kind, raw))
    for raw in patch.get("updates", []):

        def update(raw):
            identifier = resolve(raw["id"])
            row = objects[identifier]
            kind = raw["kind"]
            if kind not in UPDATE_FIELDS or row not in result[kind]:
                raise ValueError("Update kind must match the registered entity")
            if row.get("basis") != "inferred":
                raise ValueError("Recorded root goals cannot be rewritten")
            changes = deepcopy(raw["changes"])
            if not changes or set(changes) - set(UPDATE_FIELDS[kind]):
                raise ValueError("Update can only change mutable semantic fields")
            for field, value in changes.items():
                schema = PATCH["properties"][kind]["items"]["properties"][field]
                if not Draft202012Validator(schema).is_valid(value):
                    raise ValueError(f"Invalid update value for {field}")
            evidence = refs(raw["refs"])
            root = objects[root_of(identifier, objects)]
            if any(not root["origin_line"] <= ref["line_number"] <= root["end_line"] for ref in evidence):
                raise ValueError("Update crosses an effective goal revision")
            if "call_ids" in changes:
                additions = calls(changes["call_ids"], evidence)
                changes["call_ids"] = list(dict.fromkeys(row.get("call_ids", []) + additions))
            before = {key: deepcopy(row.get(key)) for key in changes}
            row.update(changes)
            row["evidence_refs"] += [ref for ref in evidence if ref not in row["evidence_refs"]]
            row["start_line"] = min(ref["line_number"] for ref in row["evidence_refs"])
            row["end_line"] = max(ref["line_number"] for ref in row["evidence_refs"])
            row.setdefault("amendments", []).append(dict(stage=stage, before=before, changes=changes, refs=evidence, rationale=raw["rationale"]))

        guarded("updates", raw, update)
    for raw in patch.get("dispositions", []):

        def disposition(raw):
            goal = objects[resolve(raw["goal_id"])]
            if goal not in result["goals"]:
                raise ValueError("Disposition needs goal")
            evidence = refs(raw["refs"])
            root = objects[root_of(goal["id"], objects)]
            if any(not root["origin_line"] <= r["line_number"] <= root["end_line"] for r in evidence):
                raise ValueError("Disposition belongs to another goal revision")
            goal.update(goal_disposition=raw["state"], completion_claim=raw["completion_claim"], disposition_refs=evidence, disposition_reason=raw["rationale"])

        guarded("dispositions", raw, disposition)
    for raw in patch.get("relations", []):

        def relation(raw):
            a, b = resolve(raw["source_id"]), resolve(raw["target_id"])
            if a == b or a not in objects or b not in objects:
                raise ValueError("Relation endpoints must be distinct registered entities")
            evidence = refs(raw["refs"])
            for endpoint in (a, b):
                if not any(overlaps(ref, cited) for ref in objects[endpoint]["evidence_refs"] for cited in evidence):
                    raise ValueError("Both relation endpoints need delivered evidence")
            if (raw["kind"] in {"adopted_by", "reopens", "retries"} or raw["kind"] == "supports" and objects[b] not in result["goals"]) and max(
                r["line_number"] for r in objects[a]["evidence_refs"]
            ) > max(r["line_number"] for r in objects[b]["evidence_refs"]):
                raise ValueError("Future evidence cannot support an earlier decision")
            if raw["kind"] == "adopted_by":
                target_lines = {r["line_number"] for r in objects[b]["evidence_refs"]}
                assertions = [
                    p
                    for p in delivered.values()
                    if p["line"] in target_lines and p["type"] == "llm.completed" and (p.get("evidence_ref") or {}).get("field_path", "").endswith("content")
                ]
                if not assertions or max(r["line_number"] for r in objects[a]["evidence_refs"]) >= max(p["line"] for p in assertions):
                    raise ValueError("Adoption requires an observable later public statement")
            result["relations"].append(
                dict(
                    id="relation:" + identity(stage, a, b, raw["kind"]),
                    source_id=a,
                    target_id=b,
                    kind=raw["kind"],
                    layer="semantic",
                    basis="inferred",
                    confidence=raw.get("confidence", "low"),
                    evidence_refs=evidence,
                    rationale=raw["rationale"],
                )
            )

        guarded("relations", raw, relation)
    for raw in patch.get("merges", []):

        def merge(raw):
            a, b = resolve(raw["source_id"]), resolve(raw["target_id"])
            if a == b or a not in objects or b not in objects:
                raise ValueError("Merge requires distinct entities")
            group = next((k for k in ("goals", "attempts") if objects[a] in result[k] and objects[b] in result[k]), None)
            if not group or objects[a]["basis"] != "inferred" or objects[b]["basis"] != "inferred":
                raise ValueError("Only inferred goals/attempts may merge")
            field = "parent_goal_id" if group == "goals" else "goal_id"
            if resolve(objects[a][field]) != resolve(objects[b][field]):
                raise ValueError("Merge requires same parent and goal revision")
            evidence = refs(raw["refs"])
            for endpoint in (a, b):
                if not any(overlaps(ref, cited) for ref in objects[endpoint]["evidence_refs"] for cited in evidence):
                    raise ValueError("Merge needs both entities evidence")
            result["aliases"][a] = b
            objects[a]["superseded_by"] = b
            for row in objects.values():
                for f in ("parent_goal_id", "goal_id", "attempt_id"):
                    if row.get(f) == a:
                        row[f] = b
            objects[b]["evidence_refs"] += [r for r in objects[a]["evidence_refs"] if r not in objects[b]["evidence_refs"]]

        guarded("merges", raw, merge)
    for raw in patch.get("diagnostics", []):

        def diagnostic(raw):
            goal = resolve(raw["goal_id"])
            if goal not in {r["id"] for r in result["goals"]}:
                raise ValueError("Diagnostic needs an existing goal")
            evidence = refs(raw["refs"])
            root = objects[root_of(goal, objects)]
            if any(not root["origin_line"] <= r["line_number"] <= root["end_line"] for r in evidence):
                raise ValueError("Diagnostic crosses a goal revision")
            ids = calls(raw["call_ids"], evidence)
            result["diagnostics"].append(
                dict(
                    id="diagnostic:" + identity(stage, raw["key"]),
                    goal_id=goal,
                    **{k: raw[k] for k in ("observation", "interpretation", "alternative", "improvement")},
                    call_ids=ids,
                    evidence_refs=evidence,
                    basis="inferred",
                    confidence=raw.get("confidence", "low"),
                    cost_meaning="involved_not_savings",
                )
            )

        guarded("diagnostics", raw, diagnostic)
    result["validation_diagnostics"].extend(issues)
    return result, referenced, issues
