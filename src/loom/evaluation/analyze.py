"""Deterministic trace evaluation analyzer and CLI."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from loom.core import Result, err, make_loom_error, ok
from loom.evaluation.artifacts import EvaluationArtifacts, write_evaluation_artifacts
from loom.evaluation.assessments import StepAssessment, assess_steps
from loom.evaluation.metrics import MetricResult, calculate_metrics
from loom.trace_analysis import EpisodeGraph, build_episode_graph, load_normalized_events


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    trace_path: Path
    out_dir: Path = Path(".loom/evaluation")

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_path", Path(self.trace_path))
        object.__setattr__(self, "out_dir", Path(self.out_dir))


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    graph: EpisodeGraph
    metrics: tuple[MetricResult, ...]
    assessments: tuple[StepAssessment, ...]
    artifacts: EvaluationArtifacts
    report: str


async def analyze_trace(config: EvaluationConfig) -> Result:
    if not config.trace_path.exists():
        return err(make_loom_error("VALIDATION_FAILED", "Trace path does not exist", retryable=False, metadata={"trace_path": str(config.trace_path)}))
    loaded = load_normalized_events(config.trace_path)
    if not loaded.ok:
        return loaded
    graph = build_episode_graph(loaded.value.events)
    metrics = calculate_metrics(graph)
    assessments = assess_steps(graph)
    try:
        artifacts = write_evaluation_artifacts(config.out_dir, graph, metrics, assessments)
    except OSError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not write evaluation artifacts",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"out_dir": str(config.out_dir)},
            )
        )
    report = artifacts.report_path.read_text(encoding="utf-8")
    return ok(EvaluationResult(graph=graph, metrics=metrics, assessments=assessments, artifacts=artifacts, report=report))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze Loom trace JSONL with deterministic evaluation metrics.")
    parser.add_argument("--trace-path", required=True, type=Path)
    parser.add_argument("--out-dir", default=Path(".loom/evaluation"), type=Path)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> EvaluationConfig:
    args = _build_parser().parse_args(argv)
    return EvaluationConfig(trace_path=args.trace_path, out_dir=args.out_dir)


def main(argv: Sequence[str] | None = None) -> None:
    result = asyncio.run(analyze_trace(parse_args(argv)))
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Trace evaluation failed")
    print(result.value.report, end="")


if __name__ == "__main__":
    main()


__all__ = ["EvaluationConfig", "EvaluationResult", "analyze_trace", "main", "parse_args"]
