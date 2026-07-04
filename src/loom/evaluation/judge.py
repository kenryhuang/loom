"""Step-level LLM judging for trace evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, ok, thaw_json
from loom.evaluation.assessments import Finding, StepAssessment
from loom.evaluation.episodes import EpisodeGraph, LlmRoundEpisode, StepGraphEpisode, ToolCallEpisode
from loom.evaluation.records import NormalizedEvent
from loom.evaluation.token_usage import total_tokens_for_events
from loom.llm import LlmMessage, TokenUsage
from loom.trace_analysis import EvidenceRef, evidence_ref_for_event

JUDGE_DIMENSIONS = (
    "task_progress",
    "instruction_following",
    "tool_selection",
    "tool_arguments",
    "tool_result_handling",
    "evidence_grounding",
    "context_quality",
    "efficiency",
    "recovery",
)


@dataclass(frozen=True, slots=True)
class MessageExcerpt:
    role: str
    content_excerpt: str


@dataclass(frozen=True, slots=True)
class LlmRoundSummary:
    id: str
    llm_call_id: str
    status: str
    model: str | None
    finish_reason: str | None
    request_messages: tuple[MessageExcerpt, ...]
    response_excerpt: str
    tool_call_count: int
    token_total: int
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_messages", tuple(self.request_messages))
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class ToolCallSummary:
    id: str
    tool_id: str
    tool_call_id: str | None
    status: str
    arguments_excerpt: str
    result_excerpt: str
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class StepEvidencePack:
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    step_status: str
    deterministic_status: str
    deterministic_score: float
    deterministic_dimensions: Mapping[str, float]
    deterministic_findings: tuple[Mapping[str, Any], ...]
    token_total: int
    llm_rounds: tuple[LlmRoundSummary, ...]
    tool_calls: tuple[ToolCallSummary, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "deterministic_dimensions", dict(self.deterministic_dimensions))
        object.__setattr__(self, "deterministic_findings", tuple(self.deterministic_findings))
        object.__setattr__(self, "llm_rounds", tuple(self.llm_rounds))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class JudgeDimensionScore:
    name: str
    score: float
    status: str
    rationale: str
    evidence_event_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class JudgeFinding:
    id: str
    severity: str
    category: str
    message: str
    dimension: str
    affected_surface: str
    recommendation: str
    confidence: float
    evidence_event_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class StepJudgeAssessment:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    overall: float
    status: str
    dimensions: Mapping[str, JudgeDimensionScore]
    findings: tuple[JudgeFinding, ...]
    confidence: float
    evaluator_model: str
    token_usage: TokenUsage
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", dict(self.dimensions))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


class LlmStepJudge:
    def __init__(self, provider: Any):
        self.provider = provider

    async def judge(self, pack: StepEvidencePack) -> Result:
        messages = build_step_judge_messages(pack)
        response = await self.provider.chat(messages, tools=None)
        if not response.ok:
            return response
        return parse_step_judge_assessment(
            response.value.content or "",
            pack,
            evaluator_model=str(getattr(self.provider, "model", "unknown")),
            token_usage=response.value.usage,
        )


def build_step_evidence_pack(graph: EpisodeGraph, step: StepGraphEpisode, assessment: StepAssessment) -> StepEvidencePack:
    events = _step_events(graph, step)
    llm_rounds = tuple(item for item in graph.llm_rounds if item.id in step.llm_round_ids)
    tool_calls = tuple(item for item in graph.tool_calls if item.id in step.tool_call_ids)
    return StepEvidencePack(
        run_id=step.run_id,
        loop_id=step.loop_id,
        trace_id=step.trace_id,
        step_number=step.step_number,
        step_status=step.status,
        deterministic_status=assessment.status,
        deterministic_score=assessment.aggregate_score,
        deterministic_dimensions={name: dimension.score for name, dimension in assessment.dimensions.items()},
        deterministic_findings=tuple(_finding_summary(finding) for finding in assessment.findings),
        token_total=total_tokens_for_events(events),
        llm_rounds=tuple(_llm_round_summary(item) for item in llm_rounds),
        tool_calls=tuple(_tool_call_summary(item) for item in tool_calls),
        evidence_refs=tuple(evidence_ref_for_event(event, max_excerpt_chars=100) for event in events if event.hash is not None),
        evidence_event_hashes=step.event_hashes,
    )


def build_step_judge_messages(pack: StepEvidencePack) -> tuple[LlmMessage, LlmMessage]:
    system = LlmMessage(
        role="system",
        content=(
            "You are a step-level evaluator for an agent loop trace. Judge only the provided compact evidence pack. "
            "Return only valid JSON with top-level keys: overall, dimensions, findings, confidence. "
            "Scores must be numbers from 0.0 to 1.0. dimensions must contain exactly these keys: "
            f"{', '.join(JUDGE_DIMENSIONS)}. Each dimension value may be a number or an object with score, rationale, "
            "status, and evidence_event_hashes. Findings should describe concrete improvement opportunities only."
        ),
    )
    user = LlmMessage(role="user", content=json.dumps(_to_plain(pack), ensure_ascii=False, sort_keys=True, indent=2))
    return (system, user)


def parse_step_judge_assessment(
    content: str,
    pack: StepEvidencePack,
    *,
    evaluator_model: str,
    token_usage: TokenUsage,
) -> Result:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        return err(_parse_error("Could not parse step judge JSON", pack, cause={"message": str(exc)}))
    if not isinstance(payload, Mapping):
        return err(_parse_error("Step judge response must be a JSON object", pack))

    overall = _validate_score(payload.get("overall"), "overall", pack)
    if not overall.ok:
        return overall
    confidence = _validate_score(payload.get("confidence"), "confidence", pack)
    if not confidence.ok:
        return confidence
    dimensions = _validate_dimensions(payload.get("dimensions"), pack)
    if not dimensions.ok:
        return dimensions
    findings = _validate_findings(payload.get("findings", ()), pack)
    if not findings.ok:
        return findings

    finding_items = findings.value
    return ok(
        StepJudgeAssessment(
            id=f"judge:{pack.run_id}:{pack.trace_id}:{pack.step_number}",
            run_id=pack.run_id,
            loop_id=pack.loop_id,
            trace_id=pack.trace_id,
            step_number=pack.step_number,
            overall=overall.value,
            status=_assessment_status(finding_items, overall.value),
            dimensions=dimensions.value,
            findings=finding_items,
            confidence=confidence.value,
            evaluator_model=evaluator_model,
            token_usage=token_usage,
            evidence_event_hashes=pack.evidence_event_hashes,
        )
    )


def _llm_round_summary(round_item: LlmRoundEpisode) -> LlmRoundSummary:
    requested = round_item.requested_event.payload if round_item.requested_event is not None else {}
    completed = round_item.completed_event.payload if round_item.completed_event is not None else {}
    response = completed.get("response") if isinstance(completed, Mapping) else None
    if not isinstance(response, Mapping):
        response = {}
    tool_calls = response.get("tool_calls")
    return LlmRoundSummary(
        id=round_item.id,
        llm_call_id=round_item.llm_call_id,
        status=round_item.status,
        model=_optional_str(completed.get("model") or requested.get("model")),
        finish_reason=_optional_str(response.get("finish_reason")),
        request_messages=_message_excerpts(requested.get("messages")),
        response_excerpt=_excerpt(response.get("content"), max_chars=240),
        tool_call_count=len(tool_calls) if isinstance(tool_calls, list | tuple) else 0,
        token_total=total_tokens_for_events((round_item.completed_event,)) if round_item.completed_event is not None else 0,
        evidence_event_hashes=round_item.event_hashes,
    )


def _tool_call_summary(tool_call: ToolCallEpisode) -> ToolCallSummary:
    started = tool_call.started_event.payload if tool_call.started_event is not None else {}
    finished_event = tool_call.completed_event or tool_call.failed_event
    finished = finished_event.payload if finished_event is not None else {}
    output = finished.get("output") or finished.get("result") or finished.get("error") or finished
    return ToolCallSummary(
        id=tool_call.id,
        tool_id=tool_call.tool_id,
        tool_call_id=tool_call.tool_call_id,
        status=tool_call.status,
        arguments_excerpt=_excerpt(started.get("input") or started.get("arguments") or started, max_chars=200),
        result_excerpt=_excerpt(_tool_output_content(output), max_chars=48),
        evidence_event_hashes=tool_call.event_hashes,
    )


def _tool_output_content(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in ("content", "stdout", "report", "stderr", "message"):
            nested = value.get(key)
            if nested:
                return nested
    return value


def _message_excerpts(messages: Any) -> tuple[MessageExcerpt, ...]:
    if not isinstance(messages, list | tuple):
        return ()
    excerpts: list[MessageExcerpt] = []
    for item in messages[:8]:
        if isinstance(item, Mapping):
            role = str(item.get("role") or "unknown")
            content = item.get("content")
        else:
            role = str(getattr(item, "role", "unknown"))
            content = getattr(item, "content", "")
        excerpts.append(MessageExcerpt(role=role, content_excerpt=_excerpt(content, max_chars=240)))
    return tuple(excerpts)


def _finding_summary(finding: Finding) -> Mapping[str, Any]:
    return {
        "severity": finding.severity,
        "category": finding.category,
        "dimension": finding.dimension,
        "affected_surface": finding.affected_surface,
        "message": finding.message,
        "evidence_event_hashes": finding.evidence_event_hashes,
    }


def _step_events(graph: EpisodeGraph, step: StepGraphEpisode) -> tuple[NormalizedEvent, ...]:
    return tuple(
        event
        for event in graph.events
        if event.run_id == step.run_id and event.trace_id == step.trace_id and event.step_number == step.step_number
    )


def _validate_dimensions(value: Any, pack: StepEvidencePack) -> Result:
    if not isinstance(value, Mapping):
        return err(_parse_error("dimensions must be a JSON object", pack))
    missing = [dimension for dimension in JUDGE_DIMENSIONS if dimension not in value]
    if missing:
        return err(_parse_error(f"dimensions missing required keys: {', '.join(missing)}", pack))

    dimensions: dict[str, JudgeDimensionScore] = {}
    for name in JUDGE_DIMENSIONS:
        parsed = _dimension_score(name, value[name], pack)
        if not parsed.ok:
            return parsed
        dimensions[name] = parsed.value
    return ok(dimensions)


def _dimension_score(name: str, value: Any, pack: StepEvidencePack) -> Result:
    if isinstance(value, Mapping):
        score = _validate_score(value.get("score"), f"dimensions.{name}.score", pack)
        if not score.ok:
            return score
        status = str(value.get("status") or _status_for_score(score.value))
        rationale = str(value.get("rationale") or "")
        hashes = _string_tuple(value.get("evidence_event_hashes", ()), f"dimensions.{name}.evidence_event_hashes", pack)
        if not hashes.ok:
            return hashes
        return ok(JudgeDimensionScore(name=name, score=score.value, status=status, rationale=rationale, evidence_event_hashes=hashes.value))
    score = _validate_score(value, f"dimensions.{name}", pack)
    if not score.ok:
        return score
    return ok(JudgeDimensionScore(name=name, score=score.value, status=_status_for_score(score.value), rationale=""))


def _validate_findings(value: Any, pack: StepEvidencePack) -> Result:
    if not isinstance(value, list | tuple):
        return err(_parse_error("findings must be an array", pack))
    findings: list[JudgeFinding] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            return err(_parse_error(f"findings[{index}] must be a JSON object", pack))
        confidence = _validate_score(item.get("confidence", 0.5), f"findings[{index}].confidence", pack)
        if not confidence.ok:
            return confidence
        hashes = _string_tuple(item.get("evidence_event_hashes", ()), f"findings[{index}].evidence_event_hashes", pack)
        if not hashes.ok:
            return hashes
        category = str(item.get("category") or "judge_finding")
        findings.append(
            JudgeFinding(
                id=f"judge-finding:{pack.run_id}:{pack.trace_id}:{pack.step_number}:{index}:{category}",
                severity=str(item.get("severity") or "warning"),
                category=category,
                message=str(item.get("message") or ""),
                dimension=str(item.get("dimension") or "task_progress"),
                affected_surface=str(item.get("affected_surface") or "unknown"),
                recommendation=str(item.get("recommendation") or ""),
                confidence=confidence.value,
                evidence_event_hashes=hashes.value,
            )
        )
    return ok(tuple(findings))


def _validate_score(value: Any, name: str, pack: StepEvidencePack) -> Result:
    if value is None:
        return err(_parse_error(f"{name} is required", pack))
    try:
        score = float(value)
    except (TypeError, ValueError):
        return err(_parse_error(f"{name} must be a number from 0.0 to 1.0", pack))
    if score < 0.0 or score > 1.0:
        return err(_parse_error(f"{name} must be a number from 0.0 to 1.0", pack))
    return ok(score)


def _string_tuple(value: Any, name: str, pack: StepEvidencePack) -> Result:
    if not isinstance(value, list | tuple):
        return err(_parse_error(f"{name} must be an array", pack))
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return err(_parse_error(f"{name} must contain only strings", pack))
        items.append(item)
    return ok(tuple(items))


def _assessment_status(findings: tuple[JudgeFinding, ...], overall: float) -> str:
    if any(item.severity == "error" for item in findings) or overall < 0.5:
        return "fail"
    if findings or overall < 0.8:
        return "warn"
    return "pass"


def _status_for_score(score: float) -> str:
    if score < 0.5:
        return "fail"
    if score < 0.8:
        return "warn"
    return "pass"


def _parse_error(message: str, pack: StepEvidencePack, *, cause: Mapping[str, Any] | None = None) -> Any:
    return make_loom_error(
        "LLM_PARSE_ERROR",
        message,
        retryable=True,
        trace_id=pack.trace_id,
        cause=cause,
        metadata={"run_id": pack.run_id, "step_number": pack.step_number},
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _excerpt(value: Any, *, max_chars: int) -> str:
    value = thaw_json(value)
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(_to_plain(value), ensure_ascii=False, sort_keys=True)
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _to_plain(value: Any) -> Any:
    value = thaw_json(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _to_plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    return str(value)


__all__ = [
    "JUDGE_DIMENSIONS",
    "JudgeDimensionScore",
    "JudgeFinding",
    "LlmRoundSummary",
    "LlmStepJudge",
    "MessageExcerpt",
    "StepEvidencePack",
    "StepJudgeAssessment",
    "ToolCallSummary",
    "build_step_evidence_pack",
    "build_step_judge_messages",
    "parse_step_judge_assessment",
]
