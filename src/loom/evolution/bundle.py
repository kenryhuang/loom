"""Evolution inputs and manifests backed by evaluation bundles."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from loom.evaluation.bundle import EvaluationFinding, LoadedEvaluationBundle, load_evaluation_bundle
from loom.evolution.proposals import EvolutionSignal

EVOLUTION_BUNDLE_SCHEMA_VERSION = "loom.evolution.bundle.v1"


@dataclass(frozen=True, slots=True)
class EvolutionBundleSummary:
    signals: int
    proposals: int
    accepted_by_gate: int
    rejected_by_gate: int = 0


@dataclass(frozen=True, slots=True)
class EvolutionBundleArtifactRefs:
    scores: str
    signals: str
    proposals: str
    report: str


@dataclass(frozen=True, slots=True)
class EvolutionBundle:
    schema_version: str
    bundle_id: str
    created_at: str
    source_evaluation_bundle: str | None
    summary: EvolutionBundleSummary
    artifacts: EvolutionBundleArtifactRefs


def signals_from_evaluation_bundle(bundle: LoadedEvaluationBundle, *, min_frequency: int = 2) -> tuple[EvolutionSignal, ...]:
    grouped: dict[str, list[EvaluationFinding]] = defaultdict(list)
    for finding in bundle.findings:
        grouped[finding.frequency_key].append(finding)

    signals: list[EvolutionSignal] = []
    for frequency_key, findings in grouped.items():
        if len(findings) < min_frequency and not _has_single_high_impact(findings):
            continue
        signals.append(_signal_from_findings(frequency_key, tuple(findings)))
    return tuple(sorted(signals, key=lambda signal: (-signal.severity, -signal.frequency, signal.surface)))


def build_evolution_bundle(
    *,
    created_at: str,
    source_evaluation_bundle: str | os.PathLike[str] | None,
    signal_count: int,
    proposal_count: int,
) -> EvolutionBundle:
    source = None if source_evaluation_bundle is None else os.fspath(source_evaluation_bundle)
    return EvolutionBundle(
        schema_version=EVOLUTION_BUNDLE_SCHEMA_VERSION,
        bundle_id=_bundle_id(source, signal_count, proposal_count),
        created_at=created_at,
        source_evaluation_bundle=source,
        summary=EvolutionBundleSummary(signals=signal_count, proposals=proposal_count, accepted_by_gate=proposal_count),
        artifacts=EvolutionBundleArtifactRefs(
            scores="step-scores.jsonl",
            signals="signals.jsonl",
            proposals="proposals.jsonl",
            report="report.md",
        ),
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_to_plain(value), indent=2, sort_keys=True), encoding="utf-8")


def _signal_from_findings(frequency_key: str, findings: tuple[EvaluationFinding, ...]) -> EvolutionSignal:
    first = findings[0]
    trace_ids = _unique(finding.step_ref.trace_id for finding in findings if finding.step_ref is not None)
    hashes = _unique(ref.event_hash for finding in findings for ref in finding.evidence_refs if ref.event_hash is not None)[:12]
    return EvolutionSignal(
        kind="evaluation_finding",
        surface=first.surface,
        severity=sum(finding.impact_score for finding in findings) / len(findings),
        frequency=len(findings),
        trace_ids=trace_ids,
        explanation=_representative_message(findings) or frequency_key,
        confidence=sum(finding.confidence for finding in findings) / len(findings),
        evidence_event_hashes=hashes,
    )


def _has_single_high_impact(findings: list[EvaluationFinding]) -> bool:
    return any(finding.impact_score >= 0.75 or finding.severity == "error" for finding in findings)


def _representative_message(findings: Iterable[EvaluationFinding]) -> str:
    counts = Counter(finding.message for finding in findings if finding.message)
    if not counts:
        return ""
    return sorted(counts, key=lambda message: (-counts[message], message))[0]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    items: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        items.append(value)
    return tuple(items)


def _bundle_id(source: str | None, signal_count: int, proposal_count: int) -> str:
    digest = hashlib.sha256("|".join((source or "", str(signal_count), str(proposal_count))).encode("utf-8")).hexdigest()[:12]
    return f"evo_{digest}"


def _to_plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _to_plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return value


__all__ = [
    "EVOLUTION_BUNDLE_SCHEMA_VERSION",
    "EvolutionBundle",
    "EvolutionBundleArtifactRefs",
    "EvolutionBundleSummary",
    "build_evolution_bundle",
    "load_evaluation_bundle",
    "signals_from_evaluation_bundle",
    "write_json",
]
