"""Deterministic metrics for Loom evaluation episode graphs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loom.core import JsonValue
from loom.evaluation.episodes import EpisodeGraph


@dataclass(frozen=True, slots=True)
class MetricResult:
    id: str
    scope: str
    subject_id: str
    name: str
    value: JsonValue
    unit: str | None
    severity: str
    evidence_event_hashes: tuple[str, ...]


def calculate_metrics(graph: EpisodeGraph) -> tuple[MetricResult, ...]:
    partial_count = _partial_count(graph)
    llm_failed_count = sum(1 for item in graph.llm_rounds if item.status == "failed")
    tool_failure_count = sum(1 for item in graph.tool_calls if item.status == "failed")
    orphaned_count = len(graph.orphaned_events)
    return (
        _metric("run", "all", "trace.completeness", _trace_completeness(graph), "ratio", "info", graph.event_hashes),
        _metric("episode", "all", "episode.partial_count", partial_count, "count", _severity_for_count(partial_count), graph.event_hashes),
        _metric("episode", "all", "episode.orphaned_count", orphaned_count, "count", _severity_for_count(orphaned_count), graph.event_hashes),
        _metric("llm_round", "all", "llm.request_count", len(graph.llm_rounds), "count", "info", graph.event_hashes),
        _metric("llm_round", "all", "llm.failed_count", llm_failed_count, "count", _severity_for_count(llm_failed_count), graph.event_hashes),
        _metric("tool_call", "all", "tool.call_count", len(graph.tool_calls), "count", "info", graph.event_hashes),
        _metric("tool_call", "all", "tool.failure_count", tool_failure_count, "count", _severity_for_count(tool_failure_count), graph.event_hashes),
        _metric("tool_call", "all", "tool.success_rate", _tool_success_rate(graph), "ratio", "info", graph.event_hashes),
        _metric("cost", "all", "cost.total_tokens", _total_tokens(graph), "tokens", "info", graph.event_hashes),
    )


def _metric(scope: str, subject_id: str, name: str, value: JsonValue, unit: str | None, severity: str, hashes: tuple[str, ...]) -> MetricResult:
    return MetricResult(
        id=f"{scope}:{subject_id}:{name}",
        scope=scope,
        subject_id=subject_id,
        name=name,
        value=value,
        unit=unit,
        severity=severity,
        evidence_event_hashes=hashes,
    )


def _trace_completeness(graph: EpisodeGraph) -> float:
    if not graph.runs and not graph.steps:
        return 0.0
    complete_runs = sum(1 for run in graph.runs if run.status == "complete")
    complete_steps = sum(1 for step in graph.steps if step.status == "complete")
    total = len(graph.runs) + len(graph.steps)
    return (complete_runs + complete_steps) / total if total else 0.0


def _partial_count(graph: EpisodeGraph) -> int:
    return (
        sum(1 for run in graph.runs if run.status == "partial")
        + sum(1 for step in graph.steps if step.status == "partial")
        + sum(1 for item in graph.llm_rounds if item.status == "partial")
        + sum(1 for item in graph.tool_calls if item.status == "partial")
    )


def _tool_success_rate(graph: EpisodeGraph) -> float:
    if not graph.tool_calls:
        return 1.0
    complete = sum(1 for item in graph.tool_calls if item.status == "complete")
    return complete / len(graph.tool_calls)


def _total_tokens(graph: EpisodeGraph) -> int:
    total = 0
    for event in graph.events:
        total += _tokens_from_value(event.payload.get("metadata", {}))
    return total


def _tokens_from_value(value: Any) -> int:
    if not isinstance(value, Mapping):
        return 0
    token_usage = value.get("tokenUsage") or value.get("token_usage")
    if isinstance(token_usage, Mapping):
        value = token_usage.get("totalTokens") or token_usage.get("total_tokens") or 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _severity_for_count(value: int) -> str:
    return "warning" if value else "info"


__all__ = ["MetricResult", "calculate_metrics"]
