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
    *,
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
        render_evolution_report(scores=score_items, signals=signal_items, proposals=proposal_items),
        encoding="utf-8",
    )
    return artifacts


def render_evolution_report(
    *,
    scores: Iterable[StepScore],
    signals: Iterable[EvolutionSignal],
    proposals: Iterable[EvolutionProposal],
) -> str:
    score_items = tuple(scores)
    signal_items = tuple(signals)
    proposal_items = tuple(proposals)

    lines = [
        "# Trace Driven Evolution Report",
        "",
        "## Summary",
        "",
        f"- scores: {len(score_items)}",
        f"- signals: {len(signal_items)}",
        f"- proposals: {len(proposal_items)}",
        "",
        "## Proposals",
    ]
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
                f"- rationale: {proposal.rationale}",
            ]
        )
    return "\n".join(lines) + "\n"


def _write_jsonl(path: Path, records: Iterable[Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(_to_plain(record), sort_keys=True))
            handle.write("\n")


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
