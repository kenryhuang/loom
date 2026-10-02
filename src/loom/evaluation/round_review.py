"""Validate evidence-bounded semantic round reviews without inventing scores."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from loom.evaluation.diagnostics import DIMENSIONS, FactAnalysis, parse_refs
from loom.evaluation.evidence_review import EvidenceReview
from loom.evaluation.evidence_store import EvidenceStore

_PROGRESS_KINDS = {"information_gain", "hypothesis_eliminated", "artifact_change", "verification",
                   "blocker_resolution", "planning", "stalled", "unknown"}
_STATUSES = {"effective", "ineffective", "mixed", "unknown"}
_STATES = ("pre_state", "intent", "action", "observed_change", "post_state")


def _evidence(values: Any, store: EvidenceStore, round_item: Mapping, review: EvidenceReview,
              boundary: int | None) -> tuple[dict, bool]:
    refs = parse_refs(values, store)
    run_id, loop_id = round_item.get("run_id"), round_item.get("loop_id")
    for ref in refs:
        event = store.event(ref)
        if (event.run_id != run_id or event.loop_id not in (None, loop_id)):
            raise ValueError("Round evidence belongs to a different run or loop scope")
    covered = bool(refs) and all(review.covers(ref) for ref in refs)
    limitation = "" if covered else "No supporting evidence supplied" if not refs else "Supporting evidence was not fully delivered for review"
    return {
        "evidence_refs": [asdict(ref) for ref in refs],
        "prior_record_refs": [asdict(ref) for ref in refs if boundary is not None and ref.line_number < boundary],
        "decision_time_refs": [asdict(ref) for ref in refs if ref.line_number == boundary and review.covers(ref)],
        "unreviewed_request_refs": [asdict(ref) for ref in refs if ref.line_number == boundary and not review.covers(ref)],
        "hindsight_refs": [asdict(ref) for ref in refs if boundary is None or ref.line_number > boundary],
        "evidence_coverage": "reviewed" if covered else "unknown",
        "limitation": limitation,
        "temporal_limitation": (
            "Earlier records establish chronology only; model visibility requires evidence in the current request."
            if boundary is not None else "Request boundary is missing; decision-time availability cannot be established"
        ),
    }, covered


def _attributable(evidence: dict, covered: bool, boundary: int | None, *, requires_request: bool = False) -> bool:
    if not covered:
        return False
    if requires_request:
        if evidence["decision_time_refs"]:
            return True
        evidence["limitation"] = ("Context effectiveness requires reviewed evidence from the current request; "
                                  "prior or later records do not establish model visibility")
    elif boundary is None:
        evidence["limitation"] = "Missing request boundary prevents attributing evidence to this round's decision or later observations"
    elif evidence["decision_time_refs"] or evidence["hindsight_refs"]:
        return True
    else:
        evidence["limitation"] = "Only prior records support this conclusion; they do not establish current-round progress or effectiveness"
    return False


def validate_round_analyses(values: Any, store: EvidenceStore, facts: FactAnalysis,
                            review: EvidenceReview) -> tuple[dict, ...]:
    """Validate model summaries, downgrading conclusions whose refs were unread.

    Empty or partial lists are accepted; the caller computes missing-round
    coverage. Reference timing is always recomputed from the factual request,
    never trusted from fields supplied by a model.
    """
    if not isinstance(values, list | tuple):
        raise ValueError("round_analyses must be an array")
    recorded = {row["id"]: row for row in facts.trajectory}
    rows, seen = [], set()
    for value in values:
        if not isinstance(value, Mapping) or not isinstance(value.get("round_id"), str) or value["round_id"] not in recorded:
            raise ValueError("Unknown round_id in round analysis")
        round_id = value["round_id"]
        if round_id in seen:
            raise ValueError("Conflicting duplicate round analysis")
        seen.add(round_id)
        for name in _STATES:
            if not isinstance(value.get(name), str):
                raise ValueError(f"Round analysis {name} must be a string")
        progress = value.get("progress_kind")
        if not isinstance(progress, str) or progress not in _PROGRESS_KINDS:
            raise ValueError("Invalid round progress_kind")
        dimensions = value.get("dimensions")
        if not isinstance(dimensions, Mapping) or set(dimensions) != set(DIMENSIONS):
            raise ValueError("Round dimensions must contain exactly the five supported dimension names")
        round_item = recorded[round_id]
        request_ref = round_item.get("request_ref")
        boundary = store.pointer(request_ref).line_number if request_ref is not None else None
        evidence, covered = _evidence(value.get("evidence_refs", []), store, round_item, review, boundary)
        covered = _attributable(evidence, covered, boundary)
        row = {"round_id": round_id, "run_id": round_item.get("run_id"), "loop_id": round_item.get("loop_id"),
               **{name: value[name] for name in _STATES}, "progress_kind": progress if covered else "unknown",
               **evidence, "epistemic_status": "inferred" if covered and progress != "unknown" else "unknown",
               "dimensions": {}}
        for name in DIMENSIONS:
            dimension = dimensions[name]
            if not isinstance(dimension, Mapping):
                raise ValueError(f"Round dimension {name} must be an object")
            status = dimension.get("status")
            if not isinstance(status, str) or status not in _STATUSES:
                raise ValueError(f"Invalid round dimension {name} status")
            if not isinstance(dimension.get("rationale"), str):
                raise ValueError(f"Round dimension {name} rationale must be a string")
            evidence, covered = _evidence(dimension.get("evidence_refs", []), store, round_item, review, boundary)
            covered = _attributable(evidence, covered, boundary, requires_request=name == "context_effectiveness")
            row["dimensions"][name] = {"status": status if covered else "unknown", "rationale": dimension["rationale"],
                                       **evidence, "epistemic_status": "inferred" if covered and status != "unknown" else "unknown"}
        rows.append(row)
    return tuple(rows)


__all__ = ["validate_round_analyses"]
