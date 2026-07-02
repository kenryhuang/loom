"""Artifacts for deterministic Loom trace evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from loom.evaluation.episodes import EpisodeGraph
from loom.evaluation.metrics import MetricResult


@dataclass(frozen=True, slots=True)
class EvaluationArtifacts:
    out_dir: Path
    episodes_path: Path
    metrics_path: Path
    report_path: Path


def write_evaluation_artifacts(out_dir: str | os.PathLike[str], graph: EpisodeGraph, metrics: Iterable[MetricResult]) -> EvaluationArtifacts:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    metric_items = tuple(metrics)
    artifacts = EvaluationArtifacts(
        out_dir=out_path,
        episodes_path=out_path / "episodes.jsonl",
        metrics_path=out_path / "metrics.jsonl",
        report_path=out_path / "report.md",
    )
    _write_jsonl(artifacts.episodes_path, (*graph.runs, *graph.steps, *graph.llm_rounds, *graph.tool_calls))
    _write_jsonl(artifacts.metrics_path, metric_items)
    artifacts.report_path.write_text(render_evaluation_report(graph, metric_items), encoding="utf-8")
    return artifacts


def render_evaluation_report(graph: EpisodeGraph, metrics: tuple[MetricResult, ...]) -> str:
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
        "",
        "## Metrics",
    ]
    for metric in metrics:
        lines.append(f"- {metric.name}: {metric.value} {metric.unit or ''}".rstrip())
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
