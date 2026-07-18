"""Deterministic trace evaluation analyzer and CLI."""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, new_loop_id, new_run_id, now_iso, ok
from loom.evaluation.artifacts import EvaluationArtifacts, write_evaluation_artifacts
from loom.evaluation.assessments import StepAssessment, assess_steps
from loom.evaluation.judge import (
    LlmRoundJudge,
    LlmStepJudge,
    RoundJudgeAssessment,
    StepJudgeAssessment,
    build_round_evidence_packs,
    build_step_evidence_pack,
)
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
class EvaluationRunOptions:
    config: EvaluationConfig
    tui: bool = False


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    graph: EpisodeGraph
    metrics: tuple[MetricResult, ...]
    assessments: tuple[StepAssessment, ...]
    round_judge_assessments: tuple[RoundJudgeAssessment, ...]
    judge_assessments: tuple[StepJudgeAssessment, ...]
    artifacts: EvaluationArtifacts
    report: str


@dataclass(frozen=True, slots=True)
class JudgeRunResult:
    round_assessments: tuple[RoundJudgeAssessment, ...]
    step_assessments: tuple[StepJudgeAssessment, ...]


async def analyze_trace(config: EvaluationConfig, *, judge_provider: Any | None = None, event_sink: Any | None = None) -> Result:
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
                "role": "trace evaluation analyzer",
                "objective": f"Evaluate trace {config.trace_path}",
                "trace_path": str(config.trace_path),
                "out_dir": str(config.out_dir),
                "judge": config.judge,
            },
            "at": now_iso(),
        },
    )
    if not started_event.ok:
        return started_event

    if not config.trace_path.exists():
        return await _finish_analysis_error(
            event_sink,
            run_id,
            loop_id,
            started,
            make_loom_error("VALIDATION_FAILED", "Trace path does not exist", retryable=False, metadata={"trace_path": str(config.trace_path)}),
        )
    loaded = load_normalized_events(config.trace_path)
    if not loaded.ok:
        return await _finish_analysis_error(event_sink, run_id, loop_id, started, loaded.error)
    graph = build_episode_graph(loaded.value.events)
    metrics = calculate_metrics(graph)
    assessments = assess_steps(graph)
    round_judge_assessments: tuple[RoundJudgeAssessment, ...] = ()
    judge_assessments: tuple[StepJudgeAssessment, ...] = ()
    step_events = await _emit_step_events(event_sink, run_id, loop_id, graph, assessments)
    if not step_events.ok:
        return step_events
    if config.judge:
        provider_result = ok(judge_provider) if judge_provider is not None else _create_judge_provider(config)
        if not provider_result.ok:
            return await _finish_analysis_error(event_sink, run_id, loop_id, started, provider_result.error, step_count=len(assessments))
        judged = await _judge_steps(graph, assessments, provider_result.value, event_sink=event_sink, run_id=run_id, loop_id=loop_id)
        if not judged.ok:
            return await _finish_analysis_error(event_sink, run_id, loop_id, started, judged.error, step_count=len(assessments))
        round_judge_assessments = judged.value.round_assessments
        judge_assessments = judged.value.step_assessments
    try:
        artifacts = write_evaluation_artifacts(
            config.out_dir,
            graph,
            metrics,
            assessments,
            judge_assessments=judge_assessments,
            round_judge_assessments=round_judge_assessments,
            source_trace_path=config.trace_path,
        )
    except OSError as exc:
        return await _finish_analysis_error(
            event_sink,
            run_id,
            loop_id,
            started,
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not write evaluation artifacts",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"out_dir": str(config.out_dir)},
            ),
            step_count=len(assessments),
        )
    report = artifacts.report_path.read_text(encoding="utf-8")
    artifact_event = await _emit_event(
        event_sink,
        {
            "type": "evaluation.artifacts.written",
            "run_id": run_id,
            "loop_id": loop_id,
            "artifacts": {
                "episodes_path": str(artifacts.episodes_path),
                "metrics_path": str(artifacts.metrics_path),
                "assessments_path": str(artifacts.assessments_path),
                "round_judge_assessments_path": str(artifacts.round_judge_assessments_path),
                "judge_assessments_path": str(artifacts.judge_assessments_path),
                "findings_path": str(artifacts.findings_path),
                "evidence_index_path": str(artifacts.evidence_index_path),
                "evaluation_bundle_path": str(artifacts.evaluation_bundle_path),
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
            "steps": len(graph.steps),
            "metric_count": len(metrics),
            "step_assessment_count": len(assessments),
            "round_judge_assessment_count": len(round_judge_assessments),
            "judge_assessment_count": len(judge_assessments),
            "finding_count": (
                sum(len(item.findings) for item in assessments)
                + sum(len(item.findings) for item in round_judge_assessments)
                + sum(len(item.findings) for item in judge_assessments)
            ),
            "duration_ms": _elapsed_ms(started),
            "at": now_iso(),
        },
    )
    if not completed.ok:
        return completed
    return ok(
        EvaluationResult(
            graph=graph,
            metrics=metrics,
            assessments=assessments,
            round_judge_assessments=round_judge_assessments,
            judge_assessments=judge_assessments,
            artifacts=artifacts,
            report=report,
        )
    )


async def _judge_steps(
    graph: EpisodeGraph,
    assessments: tuple[StepAssessment, ...],
    provider: Any,
    *,
    event_sink: Any | None = None,
    run_id: str | None = None,
    loop_id: str | None = None,
) -> Result:
    round_judge = LlmRoundJudge(provider)
    step_judge = LlmStepJudge(provider)
    assessments_by_key = {(item.run_id, item.trace_id, item.step_number): item for item in assessments}
    round_judged: list[RoundJudgeAssessment] = []
    step_judged: list[StepJudgeAssessment] = []
    for step in graph.steps:
        assessment = assessments_by_key.get((step.run_id, step.trace_id, step.step_number))
        if assessment is None:
            continue
        step_round_judged: list[RoundJudgeAssessment] = []
        for round_pack in build_round_evidence_packs(graph, step):
            round_result = await round_judge.judge(
                round_pack,
                event_sink=event_sink,
                run_id=run_id,
                loop_id=loop_id,
                llm_call_id=f"{round_pack.llm_call_id}-evaluation-round-judge",
            )
            if not round_result.ok:
                return round_result
            step_round_judged.append(round_result.value)
            round_judged.append(round_result.value)
        result = await step_judge.judge(
            build_step_evidence_pack(graph, step, assessment, round_assessments=tuple(step_round_judged)),
            event_sink=event_sink,
            run_id=run_id,
            loop_id=loop_id,
            llm_call_id=f"{step.trace_id}-evaluation-judge-llm",
        )
        if not result.ok:
            return result
        step_judged.append(result.value)
    return ok(JudgeRunResult(round_assessments=tuple(round_judged), step_assessments=tuple(step_judged)))


async def _emit_step_events(
    event_sink: Any | None,
    run_id: str,
    loop_id: str,
    graph: EpisodeGraph,
    assessments: tuple[StepAssessment, ...],
) -> Result:
    assessments_by_key = {(item.run_id, item.trace_id, item.step_number): item for item in assessments}
    for step in graph.steps:
        started = await _emit_event(
            event_sink,
            {
                "type": "step.started",
                "run_id": run_id,
                "loop_id": loop_id,
                "trace_id": step.trace_id,
                "step_number": step.step_number,
                "source_run_id": step.run_id,
                "source_loop_id": step.loop_id,
                "event_hash_count": len(step.event_hashes),
                "at": now_iso(),
            },
        )
        if not started.ok:
            return started
        assessment = assessments_by_key.get((step.run_id, step.trace_id, step.step_number))
        completed = await _emit_event(
            event_sink,
            {
                "type": "step.completed",
                "run_id": run_id,
                "loop_id": loop_id,
                "trace_id": step.trace_id,
                "step_number": step.step_number,
                "trace": {
                    "id": step.trace_id,
                    "run_id": run_id,
                    "loop_id": loop_id,
                    "step_number": step.step_number,
                    "outcome": "evaluated",
                    "source_run_id": step.run_id,
                    "source_loop_id": step.loop_id,
                    "assessment": None if assessment is None else _assessment_summary(assessment),
                },
                "at": now_iso(),
            },
        )
        if not completed.ok:
            return completed
    return ok(None)


def _assessment_summary(assessment: StepAssessment) -> dict[str, Any]:
    return {
        "status": assessment.status,
        "aggregate_score": assessment.aggregate_score,
        "dimensions": {name: dimension.score for name, dimension in assessment.dimensions.items()},
        "findings": tuple(
            {
                "severity": finding.severity,
                "category": finding.category,
                "dimension": finding.dimension,
                "message": finding.message,
            }
            for finding in assessment.findings
        ),
    }


async def _finish_analysis_error(
    event_sink: Any | None,
    run_id: str,
    loop_id: str,
    started: float,
    error: Any,
    *,
    step_count: int = 0,
) -> Result:
    loom_error = _coerce_error(error)
    completed = await _emit_event(
        event_sink,
        {
            "type": "run.completed",
            "run_id": run_id,
            "loop_id": loop_id,
            "outcome": "fail",
            "steps": step_count,
            "duration_ms": _elapsed_ms(started),
            "error": loom_error,
            "at": now_iso(),
        },
    )
    if not completed.ok:
        return completed
    return err(loom_error)


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
    return make_loom_error("INTERNAL", "Trace evaluation failed", retryable=False)


def _create_judge_provider(config: EvaluationConfig) -> Result:
    if config.config_path is None:
        return err(make_loom_error("VALIDATION_FAILED", "Judge mode requires --config so a model can be selected", retryable=False))
    loaded = load_task_config(config.config_path)
    if not loaded.ok:
        return loaded
    return create_provider_from_task_config(loaded.value, model_name=config.model_name)


async def run_evaluation_trace_with_tui(
    config: EvaluationConfig,
    *,
    judge_provider: Any | None = None,
    app_factory: Any | None = None,
) -> Result:
    from loom.tui.tui_runner import run_job_with_tui

    return await run_job_with_tui(
        lambda collector: analyze_trace(config, judge_provider=judge_provider, event_sink=collector),
        role="trace evaluation analyzer",
        goal=f"Evaluate trace {config.trace_path}",
        app_factory=app_factory,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze Loom trace JSONL with deterministic metrics and optional step-level LLM judging.")
    parser.add_argument("--trace-path", required=True, type=Path)
    parser.add_argument("--out-dir", default=Path(".loom/evaluation"), type=Path)
    parser.add_argument("--judge", action="store_true", help="Run step-level LLM judge after deterministic evaluation.")
    parser.add_argument("--config", dest="config_path", type=Path, help="Task config YAML/TOML containing named judge models.")
    parser.add_argument("--model", dest="model_name", help="Named model from --config to use as the judge.")
    parser.add_argument("--tui", action="store_true", help="Show live TUI events while the analyzer runs.")
    return parser


def _config_from_options(args: argparse.Namespace) -> EvaluationConfig:
    return EvaluationConfig(
        trace_path=args.trace_path,
        out_dir=args.out_dir,
        judge=args.judge,
        config_path=args.config_path,
        model_name=args.model_name,
    )


def parse_args(argv: Sequence[str] | None = None) -> EvaluationConfig:
    return _config_from_options(_build_parser().parse_args(argv))


def parse_run_options(argv: Sequence[str] | None = None) -> EvaluationRunOptions:
    args = _build_parser().parse_args(argv)
    return EvaluationRunOptions(config=_config_from_options(args), tui=bool(args.tui))


def main(argv: Sequence[str] | None = None) -> None:
    options = parse_run_options(argv)
    task = run_evaluation_trace_with_tui(options.config) if options.tui else analyze_trace(options.config)
    result = asyncio.run(task)
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Trace evaluation failed")
    print(result.value.report, end="")


if __name__ == "__main__":
    main()


__all__ = [
    "EvaluationConfig",
    "EvaluationResult",
    "EvaluationRunOptions",
    "analyze_trace",
    "main",
    "parse_args",
    "parse_run_options",
    "run_evaluation_trace_with_tui",
]
