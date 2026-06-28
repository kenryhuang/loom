"""Trace-driven evolution analyzer orchestration and CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.evolution.artifacts import EvolutionArtifacts, write_evolution_artifacts
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
    config_error = _validate_config(config)
    if config_error is not None:
        return err(config_error)

    if not config.trace_path.exists():
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace path does not exist",
                retryable=False,
                metadata={"trace_path": str(config.trace_path)},
            )
        )
    if not config.trace_path.is_file():
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace path must be a file",
                retryable=False,
                metadata={"trace_path": str(config.trace_path)},
            )
        )

    records_result = _load_records(config.trace_path)
    if not records_result.ok:
        return records_result

    records = records_result.value
    episodes = tuple(build_step_episodes(records))
    incomplete_episodes = tuple(episode for episode in episodes if not episode.complete)
    if incomplete_episodes:
        return err(_incomplete_episodes_error(incomplete_episodes, config.trace_path))

    if provider is None:
        provider_result = create_env_openai_provider()
        if not provider_result.ok:
            return provider_result
        provider = provider_result.value

    scorer = LlmStepScorer(provider)

    scores: list[StepScore] = []
    for episode in episodes:
        score_result = await scorer.score(episode)
        if not score_result.ok:
            return err(_score_error(score_result.error, episode))
        scores.append(score_result.value)

    score_items = tuple(scores)
    signals = aggregate_step_scores(score_items, min_frequency=config.min_signal_frequency)
    proposals = tuple(
        gate_result.value
        for proposal in generate_evolution_proposals(signals, max_proposals=config.max_proposals)
        if (gate_result := gate_proposal(proposal, ProposalGateConfig(min_confidence=config.min_confidence))).ok
    )
    artifacts_result = _write_artifacts(config.out_dir, score_items, signals, proposals)
    if not artifacts_result.ok:
        return artifacts_result

    artifacts = artifacts_result.value
    report = artifacts.report_path.read_text(encoding="utf-8")

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


def _validate_config(config: AnalyzeConfig) -> Any | None:
    if not math.isfinite(config.min_confidence) or config.min_confidence < 0.0 or config.min_confidence > 1.0:
        return make_loom_error(
            "VALIDATION_FAILED",
            "min_confidence must be finite and between 0.0 and 1.0",
            retryable=False,
            metadata={"min_confidence": config.min_confidence},
        )
    if config.min_signal_frequency < 1:
        return make_loom_error(
            "VALIDATION_FAILED",
            "min_signal_frequency must be at least 1",
            retryable=False,
            metadata={"min_signal_frequency": config.min_signal_frequency},
        )
    if config.max_proposals < 0:
        return make_loom_error(
            "VALIDATION_FAILED",
            "max_proposals must be at least 0",
            retryable=False,
            metadata={"max_proposals": config.max_proposals},
        )
    return None


def _load_records(trace_path: Path) -> Result:
    try:
        return ok(load_trace_records(trace_path))
    except json.JSONDecodeError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace JSONL is malformed",
                retryable=False,
                cause={"message": str(exc), "line": exc.lineno, "column": exc.colno},
                metadata={"trace_path": str(trace_path)},
            )
        )
    except OSError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not read trace path",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"trace_path": str(trace_path)},
            )
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not load trace records",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"trace_path": str(trace_path)},
            )
        )


def _write_artifacts(
    out_dir: Path,
    scores: tuple[StepScore, ...],
    signals: tuple[EvolutionSignal, ...],
    proposals: tuple[EvolutionProposal, ...],
) -> Result:
    try:
        return ok(write_evolution_artifacts(out_dir, scores, signals, proposals))
    except OSError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not write evolution artifacts",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"out_dir": str(out_dir)},
            )
        )
    except (TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not serialize evolution artifacts",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"out_dir": str(out_dir)},
            )
        )


def _incomplete_episodes_error(episodes: tuple[StepEpisode, ...], trace_path: Path) -> Any:
    return make_loom_error(
        "VALIDATION_FAILED",
        "Trace contains incomplete step episodes",
        retryable=False,
        metadata={
            "trace_path": str(trace_path),
            "incomplete_trace_ids": tuple(episode.trace_id for episode in episodes),
            "incomplete_steps": tuple(
                {
                    "run_id": episode.run_id,
                    "trace_id": episode.trace_id,
                    "step_number": episode.step_number,
                }
                for episode in episodes
            ),
        },
    )


def _score_error(error: Any, episode: StepEpisode) -> Any:
    metadata = {
        "run_id": episode.run_id,
        "trace_id": episode.trace_id,
        "step_number": episode.step_number,
    }
    if error is not None and error.metadata is not None:
        metadata.update(dict(error.metadata))
    return make_loom_error(
        error.code if error is not None else "LLM_FAILED",
        error.message if error is not None else "Step scoring failed",
        retryable=error.retryable if error is not None else True,
        trace_id=episode.trace_id,
        cause=error.cause if error is not None else None,
        metadata=metadata,
    )


def parse_args(argv: Sequence[str] | None = None) -> AnalyzeConfig:
    parser = argparse.ArgumentParser(description="Analyze Loom trace JSONL for evolution proposals.")
    parser.add_argument("--trace-path", required=True, type=Path)
    parser.add_argument("--out-dir", default=Path(".loom/evolution"), type=Path)
    parser.add_argument("--min-confidence", default=0.7, type=float)
    parser.add_argument("--min-signal-frequency", default=2, type=int)
    parser.add_argument("--max-proposals", default=3, type=int)
    options = parser.parse_args(argv)
    return AnalyzeConfig(
        trace_path=options.trace_path,
        out_dir=options.out_dir,
        min_confidence=options.min_confidence,
        min_signal_frequency=options.min_signal_frequency,
        max_proposals=options.max_proposals,
    )


def main(argv: Sequence[str] | None = None) -> None:
    config = parse_args(argv)
    result = asyncio.run(analyze_trace(config))
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
