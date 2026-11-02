"""Versioned acceptance identities. Text is presentation, never a foreign key."""

import hashlib
import json
from copy import deepcopy

from loom.service.contracts import ServiceError, canonical


def criteria(owner, descriptions, revision=1):
    return [dict(id="criterion_" + hashlib.sha256(f"{owner}:{i}".encode()).hexdigest()[:32],
                 revision=revision, text=description, owner_id=owner) for i, description in enumerate(descriptions)]


def key(criterion):
    return criterion["id"], criterion["revision"]


def refs(items):
    return [dict(criterion_id=c["id"], criterion_revision=c["revision"]) for c in items]


def ref_key(ref):
    return ref["criterion_id"], ref["criterion_revision"]


def merge(*groups):
    result = {}
    for group in groups:
        for item in group:
            result.setdefault(key(item), deepcopy(item))
    return list(result.values())


def initialize(record):
    if "acceptance_criteria" not in record:
        record["acceptance_criteria"] = criteria(record["id"], record.get("acceptance", []), record.get("contract_revision", 1))
    record.setdefault("contract_revision", 1)
    return record


def inherit(record, parents):
    initialize(record)
    record["acceptance_criteria"] = merge(record["acceptance_criteria"], parents)
    record["acceptance"] = list(dict.fromkeys(c["text"] for c in record["acceptance_criteria"]))


def revise(record):
    record["contract_revision"] = record.get("contract_revision", 1) + 1
    record["acceptance_criteria"] = criteria(record["id"], record["acceptance"], record["contract_revision"])


def sync_objectives(agent, previous=None):
    """Only explicit Role edits revise objective contracts; state revisions do not."""
    old = {o["id"]: o for o in (previous or {}).get("role", {}).get("long_term_objectives", [])}
    contracts = agent.setdefault("objective_contracts", {})
    for objective in agent["role"]["long_term_objectives"]:
        oid = objective["id"]
        descriptions = objective.get("stage_planning", {}).get("completion_criteria", [])
        saved = contracts.get(oid)
        changed = oid in old and descriptions != old[oid].get("stage_planning", {}).get("completion_criteria", [])
        if saved is None or changed:
            revision = saved["revision"] + 1 if saved else 1
            contracts[oid] = dict(revision=revision, criteria=criteria(agent["id"] + ":" + oid, descriptions, revision))


def objective_view(agent, objective):
    return dict(objective, acceptance_criteria=agent.get("objective_contracts", {}).get(objective["id"], {}).get("criteria", []))


def contract_key(items):
    return hashlib.sha256(canonical(sorted(refs(items), key=lambda r: (r["criterion_id"], r["criterion_revision"]))).encode()).hexdigest()


def verdict(work, run_id, session_id, cursor, gate, *, pinned=None):
    """Bind a real execution gate to the exact dispatched contract and run."""
    items = work.get("acceptance_criteria", [])
    expected = contract_key(items)
    passed = bool(items and gate and gate["type"] == "acceptance.gate.passed" and pinned == expected)
    return dict(schema_version=1, status="passed" if passed else "unknown", contract_key=expected,
                criteria=refs(items), run_id=run_id, session_id=session_id, event_cursor=cursor,
                gate_seq=gate.get("seq") if gate else None)


def recorded_verdict(db, work):
    result = work.get("acceptance_result") or {}
    if (work.get("state") != "succeeded" or work.get("superseded_contract") or result.get("status") != "passed"
            or result.get("contract_key") != contract_key(work.get("acceptance_criteria", []))
            or {ref_key(r) for r in result.get("criteria", [])} != {key(c) for c in work.get("acceptance_criteria", [])}
            or result.get("run_id") != work.get("terminal_run_id") or result.get("session_id") != work.get("session_id")
            or result.get("event_cursor") != work.get("outcome", {}).get("event_cursor")):
        return False
    row = db.execute(
        "SELECT seq,body FROM events WHERE session_id=? AND seq<=? AND json_extract(body,'$.run_id')=? "
        "AND json_extract(body,'$.type') IN ('acceptance.gate.passed','acceptance.gate.blocked') ORDER BY seq DESC LIMIT 1",
        (result["session_id"], result["event_cursor"], result["run_id"]),
    ).fetchone()
    return bool(row and row[0] == result.get("gate_seq") and json.loads(row[1])["type"] == "acceptance.gate.passed")


def support(payload):
    stage = payload.get("stage")
    required = (stage or payload["objective"]).get("acceptance_criteria", [])
    passing = [e for e in payload["evidence"] if e.get("kind") == "delivery" and e.get("passed")
               and (not stage or e.get("goal_id") in stage["goal_ids"])]
    return [dict(criterion_id=c["id"], criterion_revision=c["revision"], description=c["text"],
                 source_refs=[e["id"] for e in passing if key(c) in {ref_key(r) for r in e.get("verified_criteria", [])}]) for c in required]


class AcceptanceError(ServiceError):
    def __init__(self, code, message, **details):
        super().__init__(message, 409, code=code)
        self.details = details


def validate_completion(value, payload):
    required = support(payload)
    checks = value.get("checks", [])
    expected = {ref_key(c) for c in required}
    cited = {ref_key(c): c["source_refs"] for c in checks}
    if not expected or len(cited) != len(checks) or set(cited) != expected:
        raise AcceptanceError("ACCEPTANCE_COVERAGE", "Review must cover every required criterion ID and revision exactly once",
                              required=[{k: c[k] for k in ("criterion_id", "criterion_revision")} for c in required])
    for item in required:
        if not set(cited[ref_key(item)]) & set(item["source_refs"]):
            raise AcceptanceError("ACCEPTANCE_EVIDENCE", f"No passed execution contract cited for {item['criterion_id']} "
                                  f"revision {item['criterion_revision']}; eligible source_refs={item['source_refs']}",
                                  criterion_id=item["criterion_id"], criterion_revision=item["criterion_revision"],
                                  eligible_source_refs=item["source_refs"])
