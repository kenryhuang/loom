"""Structured evaluation bundle contracts."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, now_iso, ok
from loom.evaluation.assessments import Finding, StepAssessment
from loom.evaluation.episodes import EpisodeGraph
from loom.evaluation.judge import JudgeFinding, RoundJudgeAssessment, StepJudgeAssessment
from loom.evaluation.metrics import MetricResult
from loom.trace_analysis import EvidenceRef, evidence_ref_for_event

SCHEMA_VERSION = "loom.evaluation.bundle.v1"


@dataclass(frozen=True, slots=True)
class TraceSource:
    path: str
    kind: str = "jsonl"
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    runs: int
    steps: int
    llm_rounds: int
    tool_calls: int
    metrics: int
    step_assessments: int
    round_judge_assessments: int
    step_judge_assessments: int
    findings: int


@dataclass(frozen=True, slots=True)
class EvaluationArtifactRefs:
    episodes: str
    metrics: str
    step_assessments: str
    round_judges: str
    step_judges: str
    findings: str
    evidence_index: str
    report: str


@dataclass(frozen=True, slots=True)
class EvaluationBundle:
    schema_version: str
    bundle_id: str
    created_at: str
    source_trace: TraceSource
    summary: EvaluationSummary
    artifacts: EvaluationArtifactRefs


@dataclass(frozen=True, slots=True)
class StepRef:
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int


@dataclass(frozen=True, slots=True)
class RoundRef:
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    round_index: int
    llm_call_id: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    ref_id: str
    event_hash: str | None
    event_type: str
    subject_id: str | None
    field_path: str | None
    excerpt: str | None
    source_trace_path: str


@dataclass(frozen=True, slots=True)
class EvaluationFinding:
    id: str
    source: str
    severity: str
    category: str
    dimension: str
    surface: str
    message: str
    recommendation: str
    confidence: float
    impact_score: float
    frequency_key: str
    step_ref: StepRef | None
    round_ref: RoundRef | None
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


@dataclass(frozen=True, slots=True)
class LoadedEvaluationBundle:
    manifest_path: Path
    manifest: EvaluationBundle
    findings: tuple[EvaluationFinding, ...]
    evidence_index: tuple[EvidenceRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "evidence_index", tuple(self.evidence_index))

    @property
    def base_dir(self) -> Path:
        return self.manifest_path.parent

    def artifact_paths(self) -> dict[str, Path]:
        artifacts = self.manifest.artifacts
        return {
            "episodes": self.base_dir / artifacts.episodes,
            "metrics": self.base_dir / artifacts.metrics,
            "step_assessments": self.base_dir / artifacts.step_assessments,
            "round_judges": self.base_dir / artifacts.round_judges,
            "step_judges": self.base_dir / artifacts.step_judges,
            "findings": self.base_dir / artifacts.findings,
            "evidence_index": self.base_dir / artifacts.evidence_index,
            "report": self.base_dir / artifacts.report,
        }


def build_evaluation_bundle(
    *,
    out_dir: Path,
    graph: EpisodeGraph,
    metrics: tuple[MetricResult, ...],
    step_assessments: tuple[StepAssessment, ...],
    round_judge_assessments: tuple[RoundJudgeAssessment, ...],
    step_judge_assessments: tuple[StepJudgeAssessment, ...],
    findings: tuple[EvaluationFinding, ...],
    source_trace_path: str | os.PathLike[str] | None = None,
) -> EvaluationBundle:
    source_path = "" if source_trace_path is None else os.fspath(source_trace_path)
    return EvaluationBundle(
        schema_version=SCHEMA_VERSION,
        bundle_id=_bundle_id(source_path, graph.event_hashes),
        created_at=now_iso(),
        source_trace=TraceSource(path=source_path, sha256=_sha256_file(source_path)),
        summary=EvaluationSummary(
            runs=len(graph.runs),
            steps=len(graph.steps),
            llm_rounds=len(graph.llm_rounds),
            tool_calls=len(graph.tool_calls),
            metrics=len(metrics),
            step_assessments=len(step_assessments),
            round_judge_assessments=len(round_judge_assessments),
            step_judge_assessments=len(step_judge_assessments),
            findings=len(findings),
        ),
        artifacts=EvaluationArtifactRefs(
            episodes="episodes.jsonl",
            metrics="metrics.jsonl",
            step_assessments="step-assessments.jsonl",
            round_judges="round-judge-assessments.jsonl",
            step_judges="judge-assessments.jsonl",
            findings="findings.jsonl",
            evidence_index="evidence-index.jsonl",
            report="report.md",
        ),
    )


def normalize_evaluation_findings(
    graph: EpisodeGraph,
    step_assessments: Iterable[StepAssessment],
    round_judge_assessments: Iterable[RoundJudgeAssessment],
    step_judge_assessments: Iterable[StepJudgeAssessment],
) -> tuple[EvaluationFinding, ...]:
    event_by_hash = {event.hash: event for event in graph.events if event.hash is not None}
    findings: list[EvaluationFinding] = []
    for assessment in step_assessments:
        for item in assessment.findings:
            findings.append(_finding_from_rule(item, assessment, event_by_hash))
    for assessment in round_judge_assessments:
        for item in assessment.findings:
            findings.append(_finding_from_judge("round_judge", item, assessment, event_by_hash))
    for assessment in step_judge_assessments:
        for item in assessment.findings:
            findings.append(_finding_from_judge("step_judge", item, assessment, event_by_hash))
    return tuple(findings)


def build_evidence_index(graph: EpisodeGraph, *, source_trace_path: str | os.PathLike[str] | None = None) -> tuple[EvidenceRecord, ...]:
    source = "" if source_trace_path is None else os.fspath(source_trace_path)
    records: list[EvidenceRecord] = []
    for event in graph.events:
        ref = evidence_ref_for_event(event, field_path=_reference_field_path(event), max_excerpt_chars=240)
        records.append(
            EvidenceRecord(
                ref_id=event.hash or event.record_id,
                event_hash=ref.event_hash,
                event_type=ref.event_type,
                subject_id=ref.subject_id,
                field_path=ref.field_path,
                excerpt=ref.excerpt,
                source_trace_path=source,
            )
        )
    return tuple(records)


def load_evaluation_bundle(path: str | os.PathLike[str]) -> Result:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping) and payload.get("schema_version") != SCHEMA_VERSION:
            return err(_bundle_error("Unsupported evaluation bundle schema version", manifest_path, schema_version=payload.get("schema_version")))
        manifest = _bundle_from_plain(payload)
        if manifest.schema_version != SCHEMA_VERSION:
            return err(_bundle_error("Unsupported evaluation bundle schema version", manifest_path, schema_version=manifest.schema_version))
        loaded = LoadedEvaluationBundle(
            manifest_path=manifest_path,
            manifest=manifest,
            findings=tuple(_finding_from_plain(item) for item in _read_jsonl(manifest_path.parent / manifest.artifacts.findings)),
            evidence_index=tuple(_evidence_record_from_plain(item) for item in _read_jsonl(manifest_path.parent / manifest.artifacts.evidence_index)),
        )
        missing = [str(path) for path in loaded.artifact_paths().values() if not path.exists()]
        if missing:
            return err(_bundle_error("Evaluation bundle references missing artifact files", manifest_path, missing=tuple(missing)))
        return ok(loaded)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return err(_bundle_error("Could not load evaluation bundle", manifest_path, cause={"name": type(exc).__name__, "message": str(exc)}))


def _finding_from_rule(item: Finding, assessment: StepAssessment, event_by_hash: Mapping[str, Any]) -> EvaluationFinding:
    surface = _normalize_surface(item.affected_surface, item.category, item.dimension)
    return EvaluationFinding(
        id=f"eval-finding:rule:{item.id}",
        source="rule",
        severity=item.severity,
        category=item.category,
        dimension=item.dimension,
        surface=surface,
        message=item.message,
        recommendation="",
        confidence=1.0,
        impact_score=_impact_score(item.severity, 1.0),
        frequency_key=_frequency_key(surface, item.category, item.dimension),
        step_ref=StepRef(assessment.run_id, assessment.loop_id, assessment.trace_id, assessment.step_number),
        round_ref=None,
        evidence_refs=_evidence_refs(item.evidence_event_hashes, event_by_hash),
    )


def _finding_from_judge(
    source: str,
    item: JudgeFinding,
    assessment: RoundJudgeAssessment | StepJudgeAssessment,
    event_by_hash: Mapping[str, Any],
) -> EvaluationFinding:
    surface = _normalize_surface(item.affected_surface, item.category, item.dimension)
    evidence_hashes = item.evidence_event_hashes or assessment.evidence_event_hashes[:12]
    round_ref = None
    if isinstance(assessment, RoundJudgeAssessment):
        round_ref = RoundRef(
            assessment.run_id,
            assessment.loop_id,
            assessment.trace_id,
            assessment.step_number,
            assessment.round_index,
            assessment.llm_call_id,
        )
    return EvaluationFinding(
        id=f"eval-finding:{source}:{item.id}",
        source=source,
        severity=item.severity,
        category=item.category,
        dimension=item.dimension,
        surface=surface,
        message=item.message,
        recommendation=item.recommendation,
        confidence=item.confidence,
        impact_score=_impact_score(item.severity, item.confidence),
        frequency_key=_frequency_key(surface, item.category, item.dimension),
        step_ref=StepRef(assessment.run_id, assessment.loop_id, assessment.trace_id, assessment.step_number),
        round_ref=round_ref,
        evidence_refs=_evidence_refs(evidence_hashes, event_by_hash),
    )


def _evidence_refs(hashes: Iterable[str], event_by_hash: Mapping[str, Any]) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    for hash_value in hashes:
        event = event_by_hash.get(hash_value)
        if event is None:
            refs.append(EvidenceRef(event_hash=hash_value, event_type="unknown", subject_id=None))
            continue
        refs.append(evidence_ref_for_event(event, field_path=_reference_field_path(event), max_excerpt_chars=240))
    return tuple(refs)


def _normalize_surface(surface: str, category: str, dimension: str) -> str:
    raw = (surface or "").strip()
    if raw and raw != "unknown":
        return raw
    category_map = {
        "tool_call_missing": "tool_call_parser",
        "high_token_usage": "context_policy",
        "tool_failure": "tool_schema",
        "tool_call_incomplete": "loop_control",
        "tool_result_not_followed_by_llm": "loop_control",
    }
    if category in category_map:
        return category_map[category]
    dimension_map = {
        "tool_selection": "tool_collection",
        "tool_choice_quality": "tool_collection",
        "tool_arguments": "tool_schema",
        "tool_argument_quality": "tool_schema",
        "tool_result_handling": "loop_control",
        "round_progress": "loop_control",
        "task_progress": "loop_control",
        "evidence_grounding": "observability",
        "efficiency": "cost_policy",
        "cost_efficiency": "cost_policy",
        "instruction_following": "system_prompt",
        "prompt_following": "system_prompt",
        "context_quality": "context_policy",
    }
    return dimension_map.get(dimension, "unknown")


def _frequency_key(surface: str, category: str, dimension: str) -> str:
    return f"{surface}:{category}:{dimension}"


def _impact_score(severity: str, confidence: float) -> float:
    base = {"info": 0.1, "warning": 0.5, "error": 0.9}.get(severity, 0.5)
    return min(1.0, max(0.0, base * max(0.0, min(1.0, confidence))))


def _reference_field_path(event: Any) -> str | None:
    if event.event_type == "llm.completed":
        return "response.content"
    if event.event_type == "llm.requested":
        return "messages"
    if event.event_type == "tool.started":
        return "input"
    if event.event_type == "tool.completed":
        return "output"
    if event.event_type == "tool.failed":
        return "error"
    if event.event_type == "trace.completed":
        return "outcome"
    return None


def _bundle_id(source_path: str, hashes: tuple[str, ...]) -> str:
    digest = hashlib.sha256("|".join((source_path, *hashes)).encode("utf-8")).hexdigest()[:12]
    return f"eval_{digest}"


def _sha256_file(path: str) -> str | None:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return None
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_from_plain(value: Mapping[str, Any]) -> EvaluationBundle:
    return EvaluationBundle(
        schema_version=str(value["schema_version"]),
        bundle_id=str(value["bundle_id"]),
        created_at=str(value["created_at"]),
        source_trace=TraceSource(**value["source_trace"]),
        summary=EvaluationSummary(**value["summary"]),
        artifacts=EvaluationArtifactRefs(**value["artifacts"]),
    )


def _finding_from_plain(value: Mapping[str, Any]) -> EvaluationFinding:
    return EvaluationFinding(
        id=str(value["id"]),
        source=str(value["source"]),
        severity=str(value["severity"]),
        category=str(value["category"]),
        dimension=str(value["dimension"]),
        surface=str(value["surface"]),
        message=str(value["message"]),
        recommendation=str(value.get("recommendation") or ""),
        confidence=float(value["confidence"]),
        impact_score=float(value["impact_score"]),
        frequency_key=str(value["frequency_key"]),
        step_ref=None if value.get("step_ref") is None else StepRef(**value["step_ref"]),
        round_ref=None if value.get("round_ref") is None else RoundRef(**value["round_ref"]),
        evidence_refs=tuple(EvidenceRef(**item) for item in value.get("evidence_refs", ())),
    )


def _evidence_record_from_plain(value: Mapping[str, Any]) -> EvidenceRecord:
    return EvidenceRecord(
        ref_id=str(value["ref_id"]),
        event_hash=value.get("event_hash"),
        event_type=str(value["event_type"]),
        subject_id=value.get("subject_id"),
        field_path=value.get("field_path"),
        excerpt=value.get("excerpt"),
        source_trace_path=str(value.get("source_trace_path") or ""),
    )


def _read_jsonl(path: Path) -> tuple[Mapping[str, Any], ...]:
    return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_to_plain(value), indent=2, sort_keys=True), encoding="utf-8")


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


def _bundle_error(message: str, path: Path, **metadata: Any) -> Any:
    cause = metadata.pop("cause", None)
    return make_loom_error("VALIDATION_FAILED", message, retryable=False, cause=cause, metadata={"bundle_path": str(path), **metadata})


__all__ = [
    "EvaluationArtifactRefs",
    "EvaluationBundle",
    "EvaluationFinding",
    "EvaluationSummary",
    "EvidenceRecord",
    "LoadedEvaluationBundle",
    "RoundRef",
    "SCHEMA_VERSION",
    "StepRef",
    "TraceSource",
    "build_evaluation_bundle",
    "build_evidence_index",
    "load_evaluation_bundle",
    "normalize_evaluation_findings",
    "write_json",
]
