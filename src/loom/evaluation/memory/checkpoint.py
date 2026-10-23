"""Explicit migration of unfinished memory windows to the current validator.

Only the most recent response is replayed; complete units and accepted records
are untouched. Legacy requests lacked a schema snapshot, so reconstruct their
initial contract (before accepted-record summaries narrowed the repair request).
"""

from copy import deepcopy

from loom.evaluation.memory.contracts import protocol_for


def prepare_replay(cp, unit, version):
    if "pending_response" in unit or unit.get("validation_version") == version:
        return
    previous = next((r for r in reversed(cp["responses"])
                     if r.get("activity") == "memory" and r.get("batch_id") == unit["id"] and r.get("text")), None)
    if previous:
        unit["pending_response"] = previous["text"]
        unit["pending_finish_reason"] = previous.get("finish_reason")
        unit["pending_schema"] = deepcopy(previous.get("response_schema") or protocol_for({**unit, "accepted_records": []}).schema)
