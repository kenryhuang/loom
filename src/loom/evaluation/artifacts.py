"""Artifacts for deterministic Loom trace evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from loom.evaluation.assessments import Finding, StepAssessment
from loom.evaluation.episodes import EpisodeGraph
from loom.evaluation.judge import JudgeFinding, StepJudgeAssessment
from loom.evaluation.metrics import MetricResult


@dataclass(frozen=True, slots=True)
class EvaluationArtifacts:
    out_dir: Path
    episodes_path: Path
    metrics_path: Path
    assessments_path: Path
    judge_assessments_path: Path
    findings_path: Path
    report_path: Path


def write_evaluation_artifacts(
    out_dir: str | os.PathLike[str],
    graph: EpisodeGraph,
    metrics: Iterable[MetricResult],
    assessments: Iterable[StepAssessment] = (),
    judge_assessments: Iterable[StepJudgeAssessment] = (),
) -> EvaluationArtifacts:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    metric_items = tuple(metrics)
    assessment_items = tuple(assessments)
    judge_items = tuple(judge_assessments)
    finding_items = (
        *(finding for assessment in assessment_items for finding in assessment.findings),
        *(finding for assessment in judge_items for finding in assessment.findings),
    )
    artifacts = EvaluationArtifacts(
        out_dir=out_path,
        episodes_path=out_path / "episodes.jsonl",
        metrics_path=out_path / "metrics.jsonl",
        assessments_path=out_path / "step-assessments.jsonl",
        judge_assessments_path=out_path / "judge-assessments.jsonl",
        findings_path=out_path / "findings.jsonl",
        report_path=out_path / "report.md",
    )
    _write_jsonl(artifacts.episodes_path, (*graph.runs, *graph.steps, *graph.llm_rounds, *graph.tool_calls))
    _write_jsonl(artifacts.metrics_path, metric_items)
    _write_jsonl(artifacts.assessments_path, assessment_items)
    _write_jsonl(artifacts.judge_assessments_path, judge_items)
    _write_jsonl(artifacts.findings_path, finding_items)
    artifacts.report_path.write_text(render_evaluation_report(graph, metric_items, assessment_items, finding_items, judge_items), encoding="utf-8")
    return artifacts


def render_evaluation_report(
    graph: EpisodeGraph,
    metrics: tuple[MetricResult, ...],
    assessments: tuple[StepAssessment, ...] = (),
    findings: tuple[Finding | JudgeFinding, ...] = (),
    judge_assessments: tuple[StepJudgeAssessment, ...] = (),
) -> str:
    lines = [
        "# Trace Evaluation Report",
        "",
        "## Summary",
        "",
        f"- runs: {len(graph.runs)}",
        f"- steps: {len(graph.steps)}",
        f"- llm_rounds: {len(graph.llm_rounds)}",
        f"- tool_calls: {len(graph.tool_calls)}",
        f"- orphaned_events: {len(graph.orphaned_events)}",
        f"- metrics: {len(metrics)}",
        f"- step_assessments: {len(assessments)}",
        f"- judge_assessments: {len(judge_assessments)}",
        f"- findings: {len(findings)}",
        "",
        "## Metrics",
    ]
    for metric in metrics:
        lines.append(f"- {metric.name}: {metric.value} {metric.unit or ''}".rstrip())
    lines.extend(["", "## Step Assessments"])
    if not assessments:
        lines.append("")
        lines.append("No step assessments generated.")
    for assessment in assessments:
        lines.extend(
            [
                "",
                f"### Step {assessment.step_number} `{assessment.trace_id}`",
                "",
                f"- status: {assessment.status}",
                f"- aggregate_score: {assessment.aggregate_score:.3f}",
                "- dimensions:",
            ]
        )
        for name, dimension in assessment.dimensions.items():
            lines.append(f"  - {name}: {dimension.score:.3f} ({dimension.status})")
        if assessment.findings:
            lines.append("- findings:")
            for finding in assessment.findings:
                lines.append(f"  - {finding.severity} {finding.category}: {finding.message}")
        else:
            lines.append("- findings: none")
    if judge_assessments:
        lines.extend(["", "## LLM Judge Assessments"])
        for assessment in judge_assessments:
            lines.extend(
                [
                    "",
                    f"### Step {assessment.step_number} `{assessment.trace_id}`",
                    "",
                    f"- status: {assessment.status}",
                    f"- overall: {assessment.overall:.3f}",
                    f"- confidence: {assessment.confidence:.3f}",
                    f"- evaluator_model: {assessment.evaluator_model}",
                    "- dimensions:",
                ]
            )
            for name, dimension in assessment.dimensions.items():
                lines.append(f"  - {name}: {dimension.score:.3f} ({dimension.status})")
            if assessment.findings:
                lines.append("- findings:")
                for finding in assessment.findings:
                    lines.append(f"  - {finding.severity} {finding.category}: {finding.message}")
            else:
                lines.append("- findings: none")
    return "\n".join(lines) + "\n"


def _write_jsonl(path: Path, items: Iterable[Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(_to_plain(item), separators=(",", ":"), sort_keys=True))
            handle.write("\n")


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


__all__ = ["EvaluationArtifacts", "render_evaluation_report", "write_evaluation_artifacts"]
