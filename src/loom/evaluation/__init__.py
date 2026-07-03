"""Trace evaluation package for Loom."""

from loom.evaluation._exports import lazy_analyze_export
from loom.evaluation.artifacts import EvaluationArtifacts, render_evaluation_report, write_evaluation_artifacts
from loom.evaluation.assessments import DimensionScore, Finding, StepAssessment, assess_steps
from loom.evaluation.episodes import EpisodeGraph, LlmRoundEpisode, RunEpisode, StepGraphEpisode, ToolCallEpisode, build_episode_graph
from loom.evaluation.metrics import MetricResult, calculate_metrics
from loom.evaluation.records import NormalizedEvent, TraceIngestResult, load_normalized_events, normalize_record

__getattr__ = lazy_analyze_export(globals())

__all__ = [
    "EpisodeGraph",
    "EvaluationArtifacts",
    "EvaluationConfig",
    "EvaluationResult",
    "DimensionScore",
    "Finding",
    "LlmRoundEpisode",
    "MetricResult",
    "NormalizedEvent",
    "RunEpisode",
    "StepGraphEpisode",
    "StepAssessment",
    "ToolCallEpisode",
    "TraceIngestResult",
    "analyze_trace",
    "assess_steps",
    "build_episode_graph",
    "calculate_metrics",
    "load_normalized_events",
    "normalize_record",
    "render_evaluation_report",
    "write_evaluation_artifacts",
]
