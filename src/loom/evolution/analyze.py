"""Trace-driven evolution analyzer orchestration and CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, new_loop_id, new_run_id, now_iso, ok
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
class AnalyzeRunOptions:
    config: AnalyzeConfig
    tui: bool = False


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    episodes: tuple[StepEpisode, ...]
    scores: tuple[StepScore, ...]
    signals: tuple[EvolutionSignal, ...]
    proposals: tuple[EvolutionProposal, ...]
    artifacts: EvolutionArtifacts
    report: str


async def analyze_trace(config: AnalyzeConfig, provider: Any = None, event_sink: Any | None = None) -> Result:
    run_id = new_run_id()
    loop_id = new_loop_id()
    started = time.monotonic()
    started_event = await _emit_event(
        event_sink,
        {
            "type": "run.started",
            "run_id": run_id,
            "loop_id": loop_id,
            "context_id": str(config.trace_path),
            "metadata": {
                "role": "trace evolution analyzer",
                "objective": f"Analyze trace {config.trace_path}",
                "trace_path": str(config.trace_path),
                "out_dir": str(config.out_dir),
            },
            "at": now_iso(),
        },
    )
    if not started_event.ok:
        return started_event

    config_error = _validate_config(config)
    if config_error is not None:
        return await _finish_analysis_error(event_sink, run_id, loop_id, started, config_error)

    if not config.trace_path.exists():
        return await _finish_analysis_error(
            event_sink,
            run_id,
            loop_id,
            started,
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace path does not exist",
                retryable=False,
                metadata={"trace_path": str(config.trace_path)},
            ),
        )
    if not config.trace_path.is_file():
        return await _finish_analysis_error(
            event_sink,
            run_id,
            loop_id,
            started,
            make_loom_error(
                "VALIDATION_FAILED",
                "Trace path must be a file",
                retryable=False,
                metadata={"trace_path": str(config.trace_path)},
            ),
        )

    records_result = _load_records(config.trace_path)
    if not records_result.ok:
        return await _finish_analysis_error(event_sink, run_id, loop_id, started, records_result.error)

    records = records_result.value
    episodes = tuple(build_step_episodes(records))
    incomplete_episodes = tuple(episode for episode in episodes if not episode.complete)
    if incomplete_episodes:
        return await _finish_analysis_error(event_sink, run_id, loop_id, started, _incomplete_episodes_error(incomplete_episodes, config.trace_path))

    if provider is None:
        provider_result = create_env_openai_provider()
        if not provider_result.ok:
            return await _finish_analysis_error(event_sink, run_id, loop_id, started, provider_result.error)
        provider = provider_result.value

    scorer = LlmStepScorer(provider)

    scores: list[StepScore] = []
    for episode in episodes:
        started_step = await _emit_event(event_sink, _step_started_event(run_id, loop_id, episode))
        if not started_step.ok:
            return started_step

        score_result = await scorer.score(
            episode,
            event_sink=event_sink,
            run_id=run_id,
            loop_id=loop_id,
            llm_call_id=f"{episode.trace_id}-evolution-score-llm",
        )
        if not score_result.ok:
            score_error = _score_error(score_result.error, episode)
            completed_step = await _emit_event(event_sink, _step_completed_event(run_id, loop_id, episode, error=score_error))
            if not completed_step.ok:
                return completed_step
            return await _finish_analysis_error(
                event_sink,
                run_id,
                loop_id,
                started,
                score_error,
                steps=len(scores),
                trace_count=len(episodes),
            )
        scores.append(score_result.value)
        completed_step = await _emit_event(event_sink, _step_completed_event(run_id, loop_id, episode, score=score_result.value))
        if not completed_step.ok:
            return completed_step

    score_items = tuple(scores)
    signals = aggregate_step_scores(score_items, min_frequency=config.min_signal_frequency)
    signal_event = await _emit_event(
        event_sink,
        {
            "type": "evolution.signals.generated",
            "run_id": run_id,
            "loop_id": loop_id,
            "signal_count": len(signals),
            "signals": signals,
            "at": now_iso(),
        },
    )
    if not signal_event.ok:
        return signal_event

    proposals = tuple(
        gate_result.value
        for proposal in generate_evolution_proposals(signals, max_proposals=config.max_proposals)
        if (gate_result := gate_proposal(proposal, ProposalGateConfig(min_confidence=config.min_confidence))).ok
    )
    proposal_event = await _emit_event(
        event_sink,
        {
            "type": "evolution.proposals.generated",
            "run_id": run_id,
            "loop_id": loop_id,
            "proposal_count": len(proposals),
            "proposals": proposals,
            "at": now_iso(),
        },
    )
    if not proposal_event.ok:
        return proposal_event

    artifacts_result = _write_artifacts(config.out_dir, score_items, signals, proposals)
    if not artifacts_result.ok:
        return await _finish_analysis_error(
            event_sink,
            run_id,
            loop_id,
            started,
            artifacts_result.error,
            steps=len(score_items),
            trace_count=len(episodes),
        )

    artifacts = artifacts_result.value
    report = artifacts.report_path.read_text(encoding="utf-8")
    artifact_event = await _emit_event(
        event_sink,
        {
            "type": "evolution.artifacts.written",
            "run_id": run_id,
            "loop_id": loop_id,
            "artifacts": {
                "score_path": str(artifacts.scores_path),
                "signal_path": str(artifacts.signals_path),
                "proposal_path": str(artifacts.proposals_path),
                "report_path": str(artifacts.report_path),
            },
            "at": now_iso(),
        },
    )
    if not artifact_event.ok:
        return artifact_event

    completed = await _emit_event(
        event_sink,
        {
            "type": "run.completed",
            "run_id": run_id,
            "loop_id": loop_id,
            "outcome": "pass",
            "steps": len(score_items),
            "trace_count": len(episodes),
            "signal_count": len(signals),
            "proposal_count": len(proposals),
            "duration_ms": _elapsed_ms(started),
            "artifacts": {
                "score_path": str(artifacts.scores_path),
                "signal_path": str(artifacts.signals_path),
                "proposal_path": str(artifacts.proposals_path),
                "report_path": str(artifacts.report_path),
            },
            "at": now_iso(),
        },
    )
    if not completed.ok:
        return completed

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


async def _finish_analysis_error(
    event_sink: Any | None,
    run_id: str,
    loop_id: str,
    started: float,
    error: Any,
    *,
    steps: int = 0,
    trace_count: int = 0,
) -> Result:
    loom_error = _coerce_error(error)
    completed = await _emit_event(
        event_sink,
        {
            "type": "run.completed",
            "run_id": run_id,
            "loop_id": loop_id,
            "outcome": "fail",
            "steps": steps,
            "trace_count": trace_count,
            "duration_ms": _elapsed_ms(started),
            "error": loom_error,
            "at": now_iso(),
        },
    )
    if not completed.ok:
        return completed
    return err(loom_error)


def _step_started_event(run_id: str, loop_id: str, episode: StepEpisode) -> dict[str, Any]:
    return {
        "type": "step.started",
        "run_id": run_id,
        "loop_id": loop_id,
        "trace_id": episode.trace_id,
        "step_number": episode.step_number,
        "source_run_id": episode.run_id,
        "source_loop_id": episode.loop_id,
        "event_hash_count": len(episode.event_hashes),
        "at": now_iso(),
    }


def _step_completed_event(
    run_id: str,
    loop_id: str,
    episode: StepEpisode,
    *,
    score: StepScore | None = None,
    error: Any | None = None,
) -> dict[str, Any]:
    outcome = "scored" if error is None else "fail"
    trace: dict[str, Any] = {
        "id": episode.trace_id,
        "run_id": run_id,
        "loop_id": loop_id,
        "step_number": episode.step_number,
        "outcome": outcome,
        "source_run_id": episode.run_id,
        "source_loop_id": episode.loop_id,
    }
    if score is not None:
        trace["score"] = _score_summary(score)
        trace["decisions"] = [
            {
                "action": {
                    "kind": "score",
                    "description": f"overall={score.overall:.2f}, confidence={score.confidence:.2f}",
                },
                "reasoning": "; ".join(score.proposed_fixes) or "No fixes proposed.",
                "confidence": score.confidence,
            }
        ]
    if error is not None:
        loom_error = _coerce_error(error)
        trace["error"] = loom_error
        trace["decisions"] = [
            {
                "action": {"kind": "score", "description": "Score step failed"},
                "reasoning": loom_error.message,
                "confidence": 0.0,
            }
        ]
    return {
        "type": "step.completed",
        "run_id": run_id,
        "loop_id": loop_id,
        "trace_id": episode.trace_id,
        "step_number": episode.step_number,
        "trace": trace,
        "at": now_iso(),
    }


def _score_summary(score: StepScore) -> dict[str, Any]:
    return {
        "overall": score.overall,
        "confidence": score.confidence,
        "dimensions": dict(score.dimensions),
        "attribution": {key: tuple(values) for key, values in score.attribution.items()},
        "proposed_fixes": score.proposed_fixes,
        "token_usage": score.token_usage,
        "evaluator_model": score.evaluator_model,
    }


async def _emit_event(event_sink: Any | None, event: dict[str, Any]) -> Result:
    if event_sink is None:
        return ok(None)
    emitted = event_sink.emit(event)
    if hasattr(emitted, "__await__"):
        emitted = await emitted
    return emitted


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _coerce_error(error: Any) -> Any:
    if error is not None:
        return error
    return make_loom_error("INTERNAL", "Trace analysis failed", retryable=False)


async def run_analyze_trace_with_tui(
    config: AnalyzeConfig,
    *,
    provider: Any | None = None,
    app_factory: Any | None = None,
) -> Result:
    from loom.tui.tui_runner import run_job_with_tui

    return await run_job_with_tui(
        lambda collector: analyze_trace(config, provider=provider, event_sink=collector),
        role="trace evolution analyzer",
        goal=f"Analyze trace {config.trace_path}",
        app_factory=app_factory,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze Loom trace JSONL for evolution proposals.")
    parser.add_argument("--trace-path", required=True, type=Path)
    parser.add_argument("--out-dir", default=Path(".loom/evolution"), type=Path)
    parser.add_argument("--min-confidence", default=0.7, type=float)
    parser.add_argument("--min-signal-frequency", default=2, type=int)
    parser.add_argument("--max-proposals", default=3, type=int)
    parser.add_argument("--tui", action="store_true", help="Show live TUI events while the analyzer runs")
    return parser


def _config_from_options(options: argparse.Namespace) -> AnalyzeConfig:
    return AnalyzeConfig(
        trace_path=options.trace_path,
        out_dir=options.out_dir,
        min_confidence=options.min_confidence,
        min_signal_frequency=options.min_signal_frequency,
        max_proposals=options.max_proposals,
    )


def parse_args(argv: Sequence[str] | None = None) -> AnalyzeConfig:
    return _config_from_options(_build_parser().parse_args(argv))


def parse_run_options(argv: Sequence[str] | None = None) -> AnalyzeRunOptions:
    options = _build_parser().parse_args(argv)
    return AnalyzeRunOptions(config=_config_from_options(options), tui=bool(options.tui))


def main(argv: Sequence[str] | None = None) -> None:
    options = parse_run_options(argv)
    task = run_analyze_trace_with_tui(options.config) if options.tui else analyze_trace(options.config)
    result = asyncio.run(task)
    if not result.ok:
        message = result.error.message if result.error else "Trace analysis failed"
        raise SystemExit(message)
    print(result.value.report, end="")


if __name__ == "__main__":
    main()


__all__ = [
    "AnalyzeConfig",
    "AnalyzeResult",
    "AnalyzeRunOptions",
    "analyze_trace",
    "main",
    "parse_args",
    "parse_run_options",
    "run_analyze_trace_with_tui",
]
