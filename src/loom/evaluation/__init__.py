"""Trace evaluation package for Loom."""

from loom.evaluation.analyze import EvaluationConfig, EvaluationResult, analyze_trace
from loom.evaluation.artifacts import EvaluationArtifacts, render_evaluation_report, write_evaluation_artifacts
from loom.evaluation.episodes import EpisodeGraph, LlmRoundEpisode, RunEpisode, StepGraphEpisode, ToolCallEpisode, build_episode_graph
from loom.evaluation.metrics import MetricResult, calculate_metrics
from loom.evaluation.records import NormalizedEvent, TraceIngestResult, load_normalized_events, normalize_record

__all__ = [
    "EpisodeGraph",
    "EvaluationArtifacts",
    "EvaluationConfig",
    "EvaluationResult",
    "LlmRoundEpisode",
    "MetricResult",
    "NormalizedEvent",
    "RunEpisode",
    "StepGraphEpisode",
    "ToolCallEpisode",
    "TraceIngestResult",
    "analyze_trace",
    "build_episode_graph",
    "calculate_metrics",
    "load_normalized_events",
    "normalize_record",
    "render_evaluation_report",
    "write_evaluation_artifacts",
]
