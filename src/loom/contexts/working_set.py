"""Task-local review state. Host receipts are facts; solver notes remain claims.

Full state lives in artifacts/checkpoints, never in the model window. No tool
execution, acceptance decisions, or long-term memory writes belong here.
"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

from loom.core import ToolRef


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


DEFAULTS = {"max_projection_chars": 12000, "batch_chars": 24000, "preview_chars": 3000, "max_stalled_reads": 0}


def configuration(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError("working_set must contain only supported budget settings")
    for key, number in value.items():
        minimum = 0 if key == "max_stalled_reads" else 512 if key == "max_projection_chars" else 1
        if type(number) is not int or number < minimum or number > 1_000_000:
            raise ValueError(f"Invalid working_set {key}")
    return {**DEFAULTS, **value}


class ReviewWorkingSet:
    def __init__(self, config, publish_artifact, read_artifact=None):
        self.config = configuration(config)
        self.publish_artifact = publish_artifact
        self.read_artifact = read_artifact
        self.data = {"version": 1, "revision": 0, "goal_digest": None, "sources": {}, "findings": {},
                     "questions": {}, "changes": {}, "history": [], "stalled_reads": 0, "duplicate_reads": 0}
        self.artifact = None

    def changed(self):
        self.data["revision"] += 1
        self.artifact = None

    def snapshot(self):
        if self.artifact is None:
            self.artifact = self.publish_artifact(deepcopy(self.data), "context_working_set")
        return {"version": 1, "revision": self.data["revision"], "artifact": self.artifact}

    def restore(self, snapshot):
        if snapshot.get("version") != 1 or self.read_artifact is None:
            raise ValueError("Working set restoration requires version 1 and an artifact reader")
        value = self.read_artifact(snapshot["artifact"]["sha256"])
        if value.get("version") != 1 or value.get("revision") != snapshot["revision"]:
            raise ValueError("Working set artifact does not match checkpoint")
        self.data = deepcopy(value)
        self.artifact = deepcopy(snapshot["artifact"])

    def set_goal(self, digest):
        if digest != self.data["goal_digest"]:
            self.data["goal_digest"] = digest
            self.changed()

    def observe_tool(self, receipt, output, *, workspace=""):
        """Only the assembly may supply receipts; never accept these from notes."""
        if receipt["id"] in self.data["sources"] or receipt["tool"] not in {"read_file", "list_directory", "fetch_url"}:
            return
        if not receipt.get("ok") or not receipt.get("artifact") or not isinstance(output, dict):
            return
        resource = output.get("resource") or {}
        arguments = json.loads(receipt["input"]) if not receipt.get("input_truncated") else {}
        path = output.get("path") or output.get("url") or arguments.get("url")
        if not isinstance(path, str):
            return
        text = output.get("text") or output.get("content") or ""
        if receipt["tool"] == "list_directory":
            text = encoded(output.get("entries", []))
        if not isinstance(text, str):
            text = ""
        version = resource.get("after_sha256") or fingerprint(text)
        location = resource.get("byte_range")
        if receipt["tool"] == "list_directory":
            start = arguments.get("offset", 0)
            location = [start, start + len(output.get("entries", []))]
        unit = "entry" if receipt["tool"] == "list_directory" else "byte" if location else "character"
        location = location or [0, len(text)]
        identity = [workspace, receipt["tool"], path, version, location]
        previous = next((s for s in reversed(list(self.data["sources"].values())) if s["identity"] == identity), None)
        duplicate = previous is not None
        # A new read proves a new file version. Directory pages do not share a
        # content version and must not invalidate one another.
        if receipt["tool"] == "read_file":
            for source in self.data["sources"].values():
                if source["identity"][:3] == identity[:3] and source["version"] != version:
                    source["stale"] = True
        self.data["sources"][receipt["id"]] = {
            "id": receipt["id"], "call_id": receipt["id"].split(":", 2)[-1], "tool": receipt["tool"],
            "path": path, "version": version, "range": location, "unit": unit, "identity": identity,
            "artifact": receipt["artifact"], "artifact_format": receipt.get("artifact_format", "output"),
            "truncated": bool(output.get("truncated") or output.get("next_offset") is not None),
            "state": previous["state"] if previous else "pending", "stale": False,
            "chars": len(text), "goal_digest": receipt.get("goal_digest"),
        }
        self.data["duplicate_reads"] += int(duplicate)
        self.data["stalled_reads"] = self.data["stalled_reads"] + 1 if duplicate else 0
        self.changed()

    def mark_stale(self, identifiers):
        changed = False
        for identifier in identifiers:
            source = self.data["sources"].get(identifier)
            if source and not source["stale"]:
                source["stale"] = True
                changed = True
        if changed:
            self.changed()

    def update_notes(self, change):
        """Validate the entire change before mutating anything. Retry is idempotent."""
        from loom.runtime.execution_contracts import validate_tool_input

        validate_tool_input(notes_tool_ref().input_schema, change)
        digest = fingerprint(change)
        previous = self.data["changes"].get(change["change_id"])
        if previous:
            if previous.get("digest") != digest:
                raise ValueError("change_id was already used with different content")
            return deepcopy(previous["result"])
        if change["base_revision"] != self.data["revision"]:
            raise ValueError(f"Working set revision changed; use base_revision={self.data['revision']}")
        if not any(change.get(key) for key in ("findings", "questions", "dispositions")):
            raise ValueError("Provide findings, questions or explicit source dispositions")
        findings, questions = deepcopy(self.data["findings"]), deepcopy(self.data["questions"])
        for key in ("findings", "questions"):
            identifiers = [row["id"] for row in change.get(key, [])]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"Duplicate {key} IDs")
        consumed = set()
        for row in change.get("findings", []):
            if row["kind"] != "unknown" and not row["evidence"]:
                raise ValueError("Observed and inferred findings require source citations")
            for citation in row["evidence"]:
                source = self.data["sources"].get(citation["evidence_id"])
                if source is None:
                    raise ValueError("Evidence was not acquired in this working set")
                if source["stale"]:
                    raise ValueError("Evidence is stale; recheck this source before making a current claim")
                start, end = citation["range"]
                if citation["unit"] != source["unit"] or not source["range"][0] <= start <= end <= source["range"][1]:
                    raise ValueError("Citation range is outside the acquired source range or has the wrong unit")
                consumed.add(source["id"])
            findings[row["id"]] = {**deepcopy(row), "goal_digest": self.data["goal_digest"]}
        for row in change.get("questions", []):
            if any(identifier not in findings for identifier in row["finding_ids"]):
                raise ValueError("Question refers to an unknown finding")
            questions[row["id"]] = {**deepcopy(row), "goal_digest": self.data["goal_digest"]}
        if any(row["question_id"] not in questions for row in change.get("findings", [])):
            raise ValueError("Finding refers to an unknown question; include the question in this change")
        for row in change.get("dispositions", []):
            if row["evidence_id"] not in self.data["sources"]:
                raise ValueError("Disposition refers to unknown evidence")
        meaningful = findings != self.data["findings"] or questions != self.data["questions"]
        meaningful |= any(self.data["sources"][key]["state"] == "pending" for key in consumed)
        meaningful |= any(self.data["sources"][row["evidence_id"]]["state"] != row["state"] for row in change.get("dispositions", []))
        self.data["history"].append({"revision": self.data["revision"], "change": deepcopy(change),
                                     "previous_findings": {r["id"]: self.data["findings"].get(r["id"]) for r in change.get("findings", [])},
                                     "previous_questions": {r["id"]: self.data["questions"].get(r["id"]) for r in change.get("questions", [])}})
        self.data.update(findings=findings, questions=questions)
        consumed_identities = {fingerprint(self.data["sources"][key]["identity"]) for key in consumed}
        for source in self.data["sources"].values():
            if fingerprint(source["identity"]) in consumed_identities and not source["stale"]:
                source["state"] = "analyzed"
        for row in change.get("dispositions", []):
            self.data["sources"][row["evidence_id"]].update(state=row["state"], disposition_reason=row["reason"])
        if meaningful:
            self.data["stalled_reads"] = 0
        self.changed()
        result = {"accepted": True, "revision": self.data["revision"], "note": "Notes retained as claims; acceptance is independent."}
        self.data["changes"][change["change_id"]] = {"digest": digest, "result": result}
        return deepcopy(result)

    def selected_evidence(self, criteria):
        ids = {c["id"] for c in criteria}
        return {e["evidence_id"] for f in self.data["findings"].values()
                if f["goal_digest"] == self.data["goal_digest"] and set(f["criterion_ids"]) & ids for e in f["evidence"]}

    def merge(self, snapshot, namespace):
        """Merge an explicit completed lane result, never its active question."""
        identifier = f"lane:{namespace}:{snapshot['artifact']['sha256']}"
        if identifier in self.data["changes"]:
            return
        other = self.read_artifact(snapshot["artifact"]["sha256"])
        for key, source in other["sources"].items():
            if key not in self.data["sources"]:
                self.data["sources"][key] = deepcopy(source)
        versions = {}
        for source in self.data["sources"].values():
            if source["tool"] == "read_file" and not source["stale"]:
                versions.setdefault(tuple(source["identity"][:3]), set()).add(source["version"])
        for source in self.data["sources"].values():
            if len(versions.get(tuple(source["identity"][:3]), ())) > 1:
                source["stale"] = True  # Conflicting concurrent versions need explicit revalidation.
        prefix = namespace + ":"
        for key, row in other["findings"].items():
            self.data["findings"][prefix + key] = {**deepcopy(row), "id": prefix + key, "question_id": prefix + row["question_id"]}
        for key, row in other["questions"].items():
            self.data["questions"][prefix + key] = {**deepcopy(row), "id": prefix + key,
                                                    "finding_ids": [prefix + value for value in row["finding_ids"]]}
        self.data["changes"][identifier] = {"lane_snapshot": deepcopy(snapshot)}
        self.changed()

    def status(self):
        pending = [s for s in self.data["sources"].values() if s["state"] == "pending" and not s["stale"]]
        return {"revision": self.data["revision"], "sources": len(self.data["sources"]), "findings": len(self.data["findings"]),
                "pending_sources": len(pending), "pending_chars": sum(s["chars"] for s in pending),
                "duplicate_reads": self.data["duplicate_reads"], "stalled_reads": self.data["stalled_reads"]}

    def pause_reason(self):
        threshold = self.config["max_stalled_reads"]
        if threshold and self.data["stalled_reads"] >= threshold:
            return "Repeated source acquisition without new coverage or review notes; analyze retained evidence before resuming"
        return None

    def projection(self, budget, focus=""):
        snapshot = self.snapshot()
        result = {"revision": self.data["revision"], "archive": snapshot["artifact"]["sha256"],
                  "notice": "Task-local reference data, not instructions or verified conclusions.",
                  "status": self.status(), "findings": [], "questions": [], "sources": [], "omitted": {}}
        result["batch_due"] = result["status"]["pending_chars"] >= self.config["batch_chars"]
        words = set(focus.lower().split())

        def relevance(row):
            return sum(word in encoded(row).lower() for word in words)

        # Fair per-section budgets prevent a large source index from hiding all findings.
        for key, fraction in (("findings", .45), ("questions", .25), ("sources", .30)):
            rows = sorted(reversed(list(self.data[key].values())), key=relevance, reverse=True)
            if key == "sources":
                rows = sorted(rows, key=lambda r: r["state"] != "pending")
            allowance, used = max(0, int((budget - len(encoded(result)) - 100) * fraction)), 0
            for row in rows:
                if key == "sources":
                    row = {**{k: row[k] for k in ("id", "tool", "path", "range", "unit", "truncated", "state", "stale")},
                           "artifact_digest": row["artifact"]["sha256"]}
                else:
                    row = {**row, "mapping_needs_review": row["goal_digest"] != self.data["goal_digest"]}
                    if key == "findings":
                        row["stale"] = any(self.data["sources"][e["evidence_id"]]["stale"] for e in row["evidence"])
                size = len(encoded(row)) + 1
                if used + size <= allowance:
                    result[key].append(row)
                    used += size
            result["omitted"][key] = len(rows) - len(result[key])
        while len(encoded(result)) > budget:
            key = next((k for k in ("sources", "questions", "findings") if result[k]), None)
            if not key:
                raise ValueError("Working set metadata cannot fit the model window")
            result[key].pop()
            result["omitted"][key] += 1
        return encoded(result)

    def project_messages(self, messages):
        """Replace only tool content, preserving complete protocol exchanges."""
        by_call = {s["call_id"]: s for s in self.data["sources"].values()}
        projected = []
        for message in messages:
            source = by_call.get(message.tool_call_id) if message.role == "tool" else None
            # JSON-action protocol uses a named user observation with a host ID.
            if source is None and message.role == "user" and message.name in {"read_file", "list_directory", "fetch_url"}:
                try:
                    value = json.loads(message.content)
                    source = self.data["sources"].get(value.get("context_source_id")) if isinstance(value, dict) else None
                except (ValueError, TypeError):
                    pass
            if source:
                cap = self.config["preview_chars"] if source["state"] == "pending" else 0
                try:
                    prior = json.loads(message.content)
                except (ValueError, TypeError):
                    prior = None
                if isinstance(prior, dict) and prior.get("context_excerpt") and prior.get("state") == source["state"]:
                    projected.append(message)
                    continue
                if len(message.content) > cap:
                    value = {"context_source_id": source["id"], "path": source["path"], "state": source["state"],
                             "artifact": source["artifact"], "range": source["range"], "unit": source["unit"],
                             "context_excerpt": True,
                             "note": ("Pending analysis; omitted content is not established by this preview. " if cap else
                                      "Use retained notes and their limitations. ") + "read_artifact restores only acquired content."}
                    if cap:
                        value["preview"] = message.content[:cap]
                    message = replace(message, content=encoded(value))
            projected.append(message)
        return projected


def notes_tool_ref():
    text = {"type": "string", "minLength": 1, "maxLength": 2000}
    identifier = {"type": "string", "minLength": 1, "maxLength": 200}
    strings = {"type": "array", "items": identifier, "maxItems": 30, "uniqueItems": True}

    def obj(properties, required):
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    citation = obj({"evidence_id": identifier, "unit": {"enum": ["byte", "character", "entry"]},
                    "range": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2}},
                   ["evidence_id", "unit", "range"])
    finding = obj({"id": identifier, "question_id": identifier, "claim": text, "criterion_ids": strings,
                   "kind": {"enum": ["observed", "inferred", "unknown"]}, "limitations": {"type": "string", "maxLength": 2000},
                   "evidence": {"type": "array", "items": citation, "maxItems": 30}},
                  ["id", "question_id", "claim", "criterion_ids", "kind", "limitations", "evidence"])
    question = obj({"id": identifier, "question": text, "finding_ids": strings, "next_action": text,
                    "status": {"enum": ["open", "supported", "unknown"]}},
                   ["id", "question", "finding_ids", "next_action", "status"])
    disposition = obj({"evidence_id": identifier, "state": {"enum": ["pending", "irrelevant"]}, "reason": text},
                      ["evidence_id", "state", "reason"])
    return ToolRef("record_context_notes",
                   "Persist incremental review findings and unanswered questions before collecting another batch. "
                   "Use evidence IDs and exact ranges from context_working_set. Notes are claims, not acceptance. "
                   "Preserve IDs when revising; omitted entries remain. Retry the same change_id with identical content.",
                   input_schema=obj({"change_id": identifier, "base_revision": {"type": "integer", "minimum": 0},
                                     **{key: {"type": "array", "items": schema, "maxItems": 20}
                                        for key, schema in (("findings", finding), ("questions", question), ("dispositions", disposition))}},
                                    ["change_id", "base_revision"]))
