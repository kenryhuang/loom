"""Version two analysis contracts: evidence-backed diagnoses, never total scores."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from loom.evaluation.evidence_store import EvidencePointer, EvidenceStore

DIMENSIONS = ("context_effectiveness", "tool_effectiveness", "loop_progress", "token_efficiency", "verify_gate")
SCHEMA_VERSION = "loom.evaluation.bundle.v2"
ANALYZER_VERSION = "trace-effectiveness.1"
PROMPT_VERSION = "trace-effectiveness-rubric.1"


@dataclass(frozen=True, slots=True)
class FactAnalysis:
    task_contracts: tuple[dict[str, Any], ...] = ()
    trajectory: tuple[dict[str, Any], ...] = ()
    context_deltas: tuple[dict[str, Any], ...] = ()
    tool_uses: tuple[dict[str, Any], ...] = ()
    token_ledger: tuple[dict[str, Any], ...] = ()
    verification_evidence: tuple[dict[str, Any], ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Diagnosis:
    dimension: str
    scope: str
    observation: str
    interpretation: str
    epistemic_status: str
    supporting_refs: tuple[EvidencePointer, ...] = ()
    counterevidence_refs: tuple[EvidencePointer, ...] = ()
    evidence_coverage: str = "unknown"
    mechanism: str = "unknown"
    consequence: str = "unknown"
    improvement_hypothesis: str = ""
    preserve: str = ""
    confidence: float | None = None


def parse_diagnosis(value: Mapping[str, Any], store: EvidenceStore) -> Diagnosis:
    if not isinstance(value, Mapping):
        raise ValueError("Diagnosis must be an object")
    if value.get("dimension") not in DIMENSIONS:
        raise ValueError("Unknown diagnosis dimension")
    if value.get("epistemic_status") not in {"observed", "inferred", "hypothesis", "unknown"}:
        raise ValueError("Invalid epistemic_status")
    for name in ("scope", "observation", "interpretation"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ValueError(f"Diagnosis {name} is required")
    refs = parse_refs(value.get("supporting_refs", []), store)
    if value["epistemic_status"] != "unknown" and not refs:
        raise ValueError("Diagnosis requires supporting evidence")
    confidence = value.get("confidence")
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, int | float)
                                   or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("Invalid self-reported confidence")
    optional = {}
    for name, default in (("evidence_coverage", "unknown"), ("mechanism", "unknown"), ("consequence", "unknown"),
                          ("improvement_hypothesis", ""), ("preserve", "")):
        item = value.get(name, default)
        if not isinstance(item, str):
            raise ValueError(f"Diagnosis {name} must be a string")
        optional[name] = item
    return Diagnosis(dimension=value["dimension"], scope=value["scope"], observation=value["observation"],
                     interpretation=value["interpretation"], epistemic_status=value["epistemic_status"],
                     supporting_refs=refs, counterevidence_refs=parse_refs(value.get("counterevidence_refs", []), store),
                     confidence=confidence, **optional)


def parse_refs(value: Any, store: EvidenceStore) -> tuple[EvidencePointer, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("Evidence references must be an array")
    return tuple(store.pointer(item) for item in value)
