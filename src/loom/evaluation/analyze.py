"""Deterministic trace evaluation analyzer and CLI."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.evaluation.artifacts import EvaluationArtifacts, write_evaluation_artifacts
from loom.evaluation.assessments import StepAssessment, assess_steps
from loom.evaluation.judge import LlmStepJudge, StepJudgeAssessment, build_step_evidence_pack
from loom.evaluation.metrics import MetricResult, calculate_metrics
from loom.tasks.config import create_provider_from_task_config, load_task_config
from loom.trace_analysis import EpisodeGraph, build_episode_graph, load_normalized_events


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    trace_path: Path
    out_dir: Path = Path(".loom/evaluation")
    judge: bool = False
    config_path: Path | None = None
    model_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_path", Path(self.trace_path))
        object.__setattr__(self, "out_dir", Path(self.out_dir))
        if self.config_path is not None:
            object.__setattr__(self, "config_path", Path(self.config_path))


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    graph: EpisodeGraph
    metrics: tuple[MetricResult, ...]
    assessments: tuple[StepAssessment, ...]
    judge_assessments: tuple[StepJudgeAssessment, ...]
    artifacts: EvaluationArtifacts
    report: str


async def analyze_trace(config: EvaluationConfig, *, judge_provider: Any | None = None) -> Result:
    if not config.trace_path.exists():
        return err(make_loom_error("VALIDATION_FAILED", "Trace path does not exist", retryable=False, metadata={"trace_path": str(config.trace_path)}))
    loaded = load_normalized_events(config.trace_path)
    if not loaded.ok:
        return loaded
    graph = build_episode_graph(loaded.value.events)
    metrics = calculate_metrics(graph)
    assessments = assess_steps(graph)
    judge_assessments: tuple[StepJudgeAssessment, ...] = ()
    if config.judge:
        provider_result = ok(judge_provider) if judge_provider is not None else _create_judge_provider(config)
        if not provider_result.ok:
            return provider_result
        judged = await _judge_steps(graph, assessments, provider_result.value)
        if not judged.ok:
            return judged
        judge_assessments = judged.value
    try:
        artifacts = write_evaluation_artifacts(config.out_dir, graph, metrics, assessments, judge_assessments)
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
    return ok(
        EvaluationResult(
            graph=graph,
            metrics=metrics,
            assessments=assessments,
            judge_assessments=judge_assessments,
            artifacts=artifacts,
            report=report,
        )
    )


async def _judge_steps(graph: EpisodeGraph, assessments: tuple[StepAssessment, ...], provider: Any) -> Result:
    judge = LlmStepJudge(provider)
    assessments_by_key = {(item.run_id, item.trace_id, item.step_number): item for item in assessments}
    judged: list[StepJudgeAssessment] = []
    for step in graph.steps:
        assessment = assessments_by_key.get((step.run_id, step.trace_id, step.step_number))
        if assessment is None:
            continue
        result = await judge.judge(build_step_evidence_pack(graph, step, assessment))
        if not result.ok:
            return result
        judged.append(result.value)
    return ok(tuple(judged))


def _create_judge_provider(config: EvaluationConfig) -> Result:
    if config.config_path is None:
        return err(make_loom_error("VALIDATION_FAILED", "Judge mode requires --config so a model can be selected", retryable=False))
    loaded = load_task_config(config.config_path)
    if not loaded.ok:
        return loaded
    return create_provider_from_task_config(loaded.value, model_name=config.model_name)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze Loom trace JSONL with deterministic metrics and optional step-level LLM judging.")
    parser.add_argument("--trace-path", required=True, type=Path)
    parser.add_argument("--out-dir", default=Path(".loom/evaluation"), type=Path)
    parser.add_argument("--judge", action="store_true", help="Run step-level LLM judge after deterministic evaluation.")
    parser.add_argument("--config", dest="config_path", type=Path, help="Task config YAML/TOML containing named judge models.")
    parser.add_argument("--model", dest="model_name", help="Named model from --config to use as the judge.")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> EvaluationConfig:
    args = _build_parser().parse_args(argv)
    return EvaluationConfig(
        trace_path=args.trace_path,
        out_dir=args.out_dir,
        judge=args.judge,
        config_path=args.config_path,
        model_name=args.model_name,
    )


def main(argv: Sequence[str] | None = None) -> None:
    result = asyncio.run(analyze_trace(parse_args(argv)))
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Trace evaluation failed")
    print(result.value.report, end="")


if __name__ == "__main__":
    main()


__all__ = ["EvaluationConfig", "EvaluationResult", "analyze_trace", "main", "parse_args"]
