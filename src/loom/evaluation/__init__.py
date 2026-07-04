"""Trace evaluation package for Loom."""

from loom.evaluation._exports import lazy_analyze_export
from loom.evaluation.artifacts import EvaluationArtifacts, render_evaluation_report, write_evaluation_artifacts
from loom.evaluation.assessments import DimensionScore, Finding, StepAssessment, assess_steps
from loom.evaluation.episodes import EpisodeGraph, LlmRoundEpisode, RunEpisode, StepGraphEpisode, ToolCallEpisode, build_episode_graph
from loom.evaluation.judge import JUDGE_DIMENSIONS, JudgeFinding, LlmStepJudge, StepEvidencePack, StepJudgeAssessment, build_step_evidence_pack
from loom.evaluation.metrics import MetricResult, calculate_metrics
from loom.evaluation.records import NormalizedEvent, TraceIngestResult, load_normalized_events, normalize_record
from loom.trace_analysis import EvidenceRef

__getattr__ = lazy_analyze_export(globals())

__all__ = [
    "EpisodeGraph",
    "EvaluationArtifacts",
    "EvaluationConfig",
    "EvaluationResult",
    "DimensionScore",
    "EvidenceRef",
    "Finding",
    "JUDGE_DIMENSIONS",
    "JudgeFinding",
    "LlmRoundEpisode",
    "LlmStepJudge",
    "MetricResult",
    "NormalizedEvent",
    "RunEpisode",
    "StepGraphEpisode",
    "StepAssessment",
    "StepEvidencePack",
    "ToolCallEpisode",
    "TraceIngestResult",
    "StepJudgeAssessment",
    "analyze_trace",
    "assess_steps",
    "build_episode_graph",
    "build_step_evidence_pack",
    "calculate_metrics",
    "load_normalized_events",
    "normalize_record",
    "render_evaluation_report",
    "write_evaluation_artifacts",
]
