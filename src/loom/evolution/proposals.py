"""Proposal generation for trace-driven evolution signals."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from loom.core import err, make_loom_error, ok
from loom.evolution.scoring import StepScore

_RISK_RANKS = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True, slots=True)
class EvolutionSignal:
    kind: str
    surface: str
    severity: float
    frequency: int
    trace_ids: tuple[str, ...]
    explanation: str
    confidence: float
    evidence_event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    id: str
    surface: str
    kind: str
    title: str
    rationale: str
    created_from_trace_ids: tuple[str, ...]
    expected_impact: Mapping[str, Any]
    risk: str
    reversible: bool
    ttl_runs: int
    patch: Mapping[str, Any]
    confidence: float
    evidence_event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProposalGateConfig:
    min_confidence: float = 0.7
    max_risk: str = "medium"
    require_reversible: bool = True


def aggregate_step_scores(scores: Iterable[StepScore], min_frequency: int = 2) -> tuple[EvolutionSignal, ...]:
    grouped: dict[str, list[tuple[StepScore, tuple[str, ...]]]] = defaultdict(list)
    seen: set[tuple[str, str, int, str]] = set()
    for score in scores:
        for surface, explanations in score.attribution.items():
            normalized = _normalize_explanations(explanations)
            if not normalized:
                continue

            surface = str(surface)
            key = (score.run_id, score.trace_id, score.step_number, surface)
            if key in seen:
                continue

            seen.add(key)
            grouped[surface].append((score, normalized))

    signals: list[EvolutionSignal] = []
    for surface, items in grouped.items():
        if len(items) < min_frequency:
            continue

        trace_ids = tuple(score.trace_id for score, _ in items)
        evidence_event_hashes = tuple(hash_ for score, _ in items for hash_ in score.evidence_event_hashes)
        confidence = sum(score.confidence for score, _ in items) / len(items)
        severity = sum(1.0 - score.overall for score, _ in items) / len(items)
        explanation = _summarize_explanations(explanations for _, explanations in items)
        signals.append(
            EvolutionSignal(
                kind="repeated_attribution",
                surface=surface,
                severity=severity,
                frequency=len(items),
                trace_ids=trace_ids,
                explanation=explanation,
                confidence=confidence,
                evidence_event_hashes=evidence_event_hashes,
            )
        )

    return tuple(sorted(signals, key=lambda signal: (-signal.severity, -signal.frequency, signal.surface)))


def generate_evolution_proposals(
    signals: Iterable[EvolutionSignal],
    max_proposals: int = 3,
) -> tuple[EvolutionProposal, ...]:
    limit = max(0, max_proposals)
    return tuple(_proposal_from_signal(signal) for signal in tuple(signals)[:limit])


def gate_proposal(proposal: EvolutionProposal, config: ProposalGateConfig | None = None):
    config = config or ProposalGateConfig()
    if config.max_risk not in _RISK_RANKS:
        return err(
            make_loom_error(
                "MUTATION_REJECTED",
                "Proposal gate max_risk is invalid",
                retryable=False,
                metadata={"proposal_id": proposal.id, "surface": proposal.surface, "max_risk": config.max_risk},
            )
        )
    if proposal.confidence < config.min_confidence:
        return err(
            make_loom_error(
                "MUTATION_REJECTED",
                "Proposal confidence is below the configured gate",
                retryable=False,
                metadata={"proposal_id": proposal.id, "surface": proposal.surface},
            )
        )
    if config.require_reversible and not proposal.reversible:
        return err(
            make_loom_error(
                "MUTATION_REJECTED",
                "Proposal is not reversible",
                retryable=False,
                metadata={"proposal_id": proposal.id, "surface": proposal.surface},
            )
        )
    if proposal.risk not in _RISK_RANKS:
        return err(
            make_loom_error(
                "MUTATION_REJECTED",
                "Proposal risk is invalid",
                retryable=False,
                metadata={"proposal_id": proposal.id, "surface": proposal.surface, "risk": proposal.risk},
            )
        )
    if _risk_rank(proposal.risk) > _risk_rank(config.max_risk):
        return err(
            make_loom_error(
                "MUTATION_REJECTED",
                "Proposal risk exceeds the configured gate",
                retryable=False,
                metadata={"proposal_id": proposal.id, "surface": proposal.surface},
            )
        )
    return ok(proposal)


def _proposal_from_signal(signal: EvolutionSignal) -> EvolutionProposal:
    kind, patch = _proposal_kind_and_patch(signal)
    return EvolutionProposal(
        id=_proposal_id(signal),
        surface=signal.surface,
        kind=kind,
        title=f"Address repeated {signal.surface} attribution",
        rationale=signal.explanation,
        created_from_trace_ids=signal.trace_ids,
        expected_impact={"severity": signal.severity, "frequency": signal.frequency},
        risk="low",
        reversible=True,
        ttl_runs=10,
        patch=patch,
        confidence=signal.confidence,
        evidence_event_hashes=signal.evidence_event_hashes,
    )


def _proposal_kind_and_patch(signal: EvolutionSignal) -> tuple[str, Mapping[str, Any]]:
    if signal.surface in {"system_prompt", "user_prompt"}:
        return (
            "prompt_rule",
            {
                "operation": "add_rule",
                "surface": signal.surface,
                "rule": signal.explanation,
            },
        )
    if signal.surface in {"tool_schema", "tool_description"}:
        return (
            "tool_schema_clarification",
            {
                "operation": "clarify_schema",
                "surface": signal.surface,
                "clarification": signal.explanation,
            },
        )
    if signal.surface == "tool_collection":
        return (
            "tool_collection_policy",
            {
                "operation": "adjust_resolver_priority",
                "surface": signal.surface,
                "reason": signal.explanation,
            },
        )
    if signal.surface == "skill_context":
        return (
            "skill_proposal",
            {
                "operation": "propose_skill",
                "surface": signal.surface,
                "reason": signal.explanation,
            },
        )
    return (
        "context_policy",
        {
            "operation": "adjust_context_policy",
            "surface": signal.surface,
            "reason": signal.explanation,
        },
    )


def _summarize_explanations(explanation_groups: Iterable[tuple[str, ...]]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for explanations in explanation_groups:
        for explanation in explanations:
            counts[explanation] += 1
    if not counts:
        return "Repeated attribution without explanation."
    return sorted(counts, key=lambda item: (-counts[item], item))[0]


def _normalize_explanations(explanations: Iterable[str]) -> tuple[str, ...]:
    return tuple(explanation.strip() for explanation in explanations if explanation.strip())


def _proposal_id(signal: EvolutionSignal) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                signal.kind,
                signal.surface,
                ",".join(sorted(signal.trace_ids)),
                signal.explanation,
            )
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"evo-proposal-{digest}"


def _risk_rank(risk: str) -> int:
    return _RISK_RANKS.get(risk, _RISK_RANKS["high"])


__all__ = [
    "EvolutionProposal",
    "EvolutionSignal",
    "ProposalGateConfig",
    "aggregate_step_scores",
    "gate_proposal",
    "generate_evolution_proposals",
]
