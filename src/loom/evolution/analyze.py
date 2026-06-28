"""Trace-driven evolution analyzer orchestration and CLI."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.evolution.artifacts import EvolutionArtifacts, render_evolution_report, write_evolution_artifacts
from loom.evolution.episodes import StepEpisode, build_step_episodes, load_trace_records
from loom.evolution.proposals import (
    EvolutionProposal,
    EvolutionSignal,
    ProposalGateConfig,
    aggregate_step_scores,
    gate_proposal,
    generate_evolution_proposals,
)
from loom.evolution.scoring import LlmStepScorer, StepScore
from loom.llm import create_env_openai_provider


@dataclass(frozen=True, slots=True)
class AnalyzeConfig:
    trace_path: Path
    out_dir: Path = Path(".loom/evolution")
    min_confidence: float = 0.7
    min_signal_frequency: int = 2
    max_proposals: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_path", Path(self.trace_path))
        object.__setattr__(self, "out_dir", Path(self.out_dir))


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    episodes: tuple[StepEpisode, ...]
    scores: tuple[StepScore, ...]
    signals: tuple[EvolutionSignal, ...]
    proposals: tuple[EvolutionProposal, ...]
    artifacts: EvolutionArtifacts
    report: str


async def analyze_trace(config: AnalyzeConfig, provider: Any = None) -> Result:
    if not config.trace_path.exists():
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace path does not exist",
                retryable=False,
                metadata={"trace_path": str(config.trace_path)},
            )
        )

    if provider is None:
        provider_result = create_env_openai_provider()
        if not provider_result.ok:
            return provider_result
        provider = provider_result.value

    records = load_trace_records(config.trace_path)
    episodes = tuple(build_step_episodes(records))
    scorer = LlmStepScorer(provider)

    scores: list[StepScore] = []
    for episode in episodes:
        score_result = await scorer.score(episode)
        if score_result.ok:
            scores.append(score_result.value)

    score_items = tuple(scores)
    signals = aggregate_step_scores(score_items, min_frequency=config.min_signal_frequency)
    proposals = tuple(
        gate_result.value
        for proposal in generate_evolution_proposals(signals, max_proposals=config.max_proposals)
        if (gate_result := gate_proposal(proposal, ProposalGateConfig(min_confidence=config.min_confidence))).ok
    )
    artifacts = write_evolution_artifacts(config.out_dir, score_items, signals, proposals)
    report = render_evolution_report(score_items, signals, proposals)

    return ok(
        AnalyzeResult(
            episodes=episodes,
            scores=score_items,
            signals=signals,
            proposals=proposals,
            artifacts=artifacts,
            report=report,
        )
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Loom trace JSONL for evolution proposals.")
    parser.add_argument("--trace-path", required=True, type=Path)
    parser.add_argument("--out-dir", default=Path(".loom/evolution"), type=Path)
    parser.add_argument("--min-confidence", default=0.7, type=float)
    parser.add_argument("--min-signal-frequency", default=2, type=int)
    parser.add_argument("--max-proposals", default=3, type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    options = parse_args(argv)
    result = asyncio.run(
        analyze_trace(
            AnalyzeConfig(
                trace_path=options.trace_path,
                out_dir=options.out_dir,
                min_confidence=options.min_confidence,
                min_signal_frequency=options.min_signal_frequency,
                max_proposals=options.max_proposals,
            )
        )
    )
    if not result.ok:
        message = result.error.message if result.error else "Trace analysis failed"
        raise SystemExit(message)
    print(result.value.report, end="")


if __name__ == "__main__":
    main()


__all__ = [
    "AnalyzeConfig",
    "AnalyzeResult",
    "analyze_trace",
    "main",
    "parse_args",
]
