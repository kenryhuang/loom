"""Persistence helpers for trace-driven evolution analysis artifacts."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from loom.evolution.proposals import EvolutionProposal, EvolutionSignal
from loom.evolution.scoring import StepScore
from loom.llm import TokenUsage


@dataclass(frozen=True, slots=True)
class EvolutionArtifacts:
    out_dir: Path
    scores_path: Path
    signals_path: Path
    proposals_path: Path
    report_path: Path


def write_evolution_artifacts(
    out_dir: os.PathLike[str] | str,
    scores: Iterable[StepScore],
    signals: Iterable[EvolutionSignal],
    proposals: Iterable[EvolutionProposal],
) -> EvolutionArtifacts:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    score_items = tuple(scores)
    signal_items = tuple(signals)
    proposal_items = tuple(proposals)

    artifacts = EvolutionArtifacts(
        out_dir=out_path,
        scores_path=out_path / "step-scores.jsonl",
        signals_path=out_path / "signals.jsonl",
        proposals_path=out_path / "proposals.jsonl",
        report_path=out_path / "report.md",
    )
    _write_jsonl(artifacts.scores_path, score_items)
    _write_jsonl(artifacts.signals_path, signal_items)
    _write_jsonl(artifacts.proposals_path, proposal_items)
    artifacts.report_path.write_text(
        render_evolution_report(score_items, signal_items, proposal_items),
        encoding="utf-8",
    )
    return artifacts


def render_evolution_report(
    scores: Iterable[StepScore],
    signals: Iterable[EvolutionSignal],
    proposals: Iterable[EvolutionProposal],
) -> str:
    score_items = tuple(scores)
    signal_items = tuple(signals)
    proposal_items = tuple(proposals)
    score_stats = _score_stats(score_items)

    lines = [
        "# Trace Driven Evolution Report",
        "",
        "## Summary",
        "",
        f"- scores: {len(score_items)}",
        f"- signals: {len(signal_items)}",
        f"- proposals: {len(proposal_items)}",
        f"- average_overall: {score_stats['average_overall']}",
        f"- lowest_overall: {score_stats['lowest_overall']}",
        "",
        "## Signals",
    ]
    if not signal_items:
        lines.extend(["", _empty_signal_explanation(score_items)])
    for signal in signal_items:
        lines.extend(
            [
                "",
                f"### {signal.surface}",
                "",
                f"- surface: {signal.surface}",
                f"- kind: {signal.kind}",
                f"- severity: {signal.severity}",
                f"- frequency: {signal.frequency}",
                f"- confidence: {signal.confidence}",
                f"- trace_ids: {_format_sequence(signal.trace_ids)}",
                f"- evidence_event_hashes: {_format_sequence(signal.evidence_event_hashes)}",
                f"- explanation: {signal.explanation}",
            ]
        )
    lines.extend(
        [
            "",
            "## Proposals",
        ]
    )
    if not proposal_items:
        lines.extend(["", "No proposals generated."])
    for proposal in proposal_items:
        lines.extend(
            [
                "",
                f"### {proposal.title}",
                "",
                f"- id: {proposal.id}",
                f"- surface: {proposal.surface}",
                f"- kind: {proposal.kind}",
                f"- risk: {proposal.risk}",
                f"- confidence: {proposal.confidence}",
                f"- created_from_trace_ids: {_format_sequence(proposal.created_from_trace_ids)}",
                f"- evidence_event_hashes: {_format_sequence(proposal.evidence_event_hashes)}",
                f"- expected_impact: {_to_json(proposal.expected_impact)}",
                f"- reversible: {proposal.reversible}",
                f"- ttl_runs: {proposal.ttl_runs}",
                f"- rationale: {proposal.rationale}",
                "",
                "```json",
                _to_json(proposal.patch, indent=2),
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


def _write_jsonl(path: Path, records: Iterable[Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(_to_plain(record), separators=(",", ":"), sort_keys=True))
            handle.write("\n")


def _score_stats(scores: tuple[StepScore, ...]) -> Mapping[str, str]:
    if not scores:
        return {"average_overall": "n/a", "lowest_overall": "n/a"}
    average = sum(score.overall for score in scores) / len(scores)
    lowest = min(score.overall for score in scores)
    return {"average_overall": f"{average:.2f}", "lowest_overall": f"{lowest:.2f}"}


def _empty_signal_explanation(scores: tuple[StepScore, ...]) -> str:
    if not scores:
        return "No signals generated because no step scores were available."
    return (
        f"No signals generated after scoring {len(scores)} step(s). "
        "This means no repeated low-quality or improvement attribution met the gate. "
        "Inspect step-scores.jsonl for raw evaluator scores and attribution."
    )


def _format_sequence(values: Iterable[Any]) -> str:
    items = tuple(values)
    if not items:
        return "(none)"
    return ", ".join(str(item) for item in items)


def _to_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(_to_plain(value), indent=indent, sort_keys=True)


def _to_plain(value: Any) -> Any:
    if isinstance(value, TokenUsage):
        return {
            "prompt_tokens": value.prompt_tokens,
            "completion_tokens": value.completion_tokens,
            "total_tokens": value.total_tokens,
        }
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
    "EvolutionArtifacts",
    "render_evolution_report",
    "write_evolution_artifacts",
]
