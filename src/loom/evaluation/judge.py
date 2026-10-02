"""Step-level LLM judging for trace evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, now_iso, ok, thaw_json
from loom.evaluation.assessments import Finding, StepAssessment
from loom.evaluation.episodes import EpisodeGraph, LlmRoundEpisode, StepGraphEpisode, ToolCallEpisode
from loom.evaluation.records import NormalizedEvent
from loom.evaluation.token_usage import total_tokens_for_events
from loom.llm import LlmMessage, TokenUsage, request_llm_response
from loom.trace_analysis import EvidenceRef, evidence_ref_for_event
from loom.trace_analysis.links import linked_tools_for_round, tool_output

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

ROUND_JUDGE_DIMENSIONS = (
    "round_progress",
    "instruction_following",
    "tool_selection",
    "tool_arguments",
    "tool_result_handling",
    "evidence_grounding",
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
class RoundEvidencePack:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    round_index: int
    llm_call_id: str
    status: str
    model: str | None
    finish_reason: str | None
    request_messages: tuple[MessageExcerpt, ...]
    response_excerpt: str
    tool_call_count: int
    token_total: int
    tool_calls: tuple[ToolCallSummary, ...]
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_messages", tuple(self.request_messages))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class RoundJudgeAssessment:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    round_index: int
    round_id: str
    llm_call_id: str
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


@dataclass(frozen=True, slots=True)
class RoundJudgeSummary:
    round_id: str
    llm_call_id: str
    round_index: int
    status: str
    overall: float
    confidence: float
    dimensions: Mapping[str, float]
    findings: tuple[Mapping[str, Any], ...]
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", dict(self.dimensions))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class StepEvidencePack:
    id: str
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
    round_count: int
    tool_call_count: int
    round_assessments: tuple[RoundJudgeSummary, ...]
    selected_evidence_refs: tuple[EvidenceRef, ...]
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "deterministic_dimensions", dict(self.deterministic_dimensions))
        object.__setattr__(self, "deterministic_findings", tuple(self.deterministic_findings))
        object.__setattr__(self, "round_assessments", tuple(self.round_assessments))
        object.__setattr__(self, "selected_evidence_refs", tuple(self.selected_evidence_refs))
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
    def __init__(self, provider: Any, *, stream: bool = False):
        self.provider = provider
        self.stream = stream

    async def judge(
        self,
        pack: StepEvidencePack,
        *,
        event_sink: Any | None = None,
        run_id: str | None = None,
        loop_id: str | None = None,
        llm_call_id: str | None = None,
    ) -> Result:
        messages = build_step_judge_messages(pack)
        evaluator_model = str(getattr(self.provider, "model", "unknown"))
        call_id = llm_call_id or f"{pack.trace_id}-evaluation-judge-llm"
        event_base = {
            "run_id": run_id or pack.run_id,
            "loop_id": loop_id or pack.loop_id,
            "trace_id": pack.trace_id,
            "llm_call_id": call_id,
            "step_number": pack.step_number,
            "model": evaluator_model,
            "source_run_id": pack.run_id,
            "source_loop_id": pack.loop_id,
        }
        requested = await _emit_event(
            event_sink,
            {
                "type": "llm.requested",
                **event_base,
                "messages": messages,
                "tools": None,
                "at": now_iso(),
            },
        )
        if not requested.ok:
            return requested

        response = await request_llm_response(
            self.provider,
            messages,
            tools=None,
            stream=self.stream,
            emit_event=lambda event: _emit_event(event_sink, event),
            event_metadata=event_base,
            now=now_iso,
        )
        if not response.ok:
            failed = await _emit_event(
                event_sink,
                {
                    "type": "llm.failed",
                    **event_base,
                    "error": response.error,
                    "at": now_iso(),
                },
            )
            if not failed.ok:
                return failed
            return response
        completed = await _emit_event(
            event_sink,
            {
                "type": "llm.completed",
                **event_base,
                "response": response.value,
                "at": now_iso(),
            },
        )
        if not completed.ok:
            return completed
        return parse_step_judge_assessment(
            response.value.content or "",
            pack,
            evaluator_model=evaluator_model,
            token_usage=response.value.usage,
        )


class LlmRoundJudge:
    def __init__(self, provider: Any, *, stream: bool = False):
        self.provider = provider
        self.stream = stream

    async def judge(
        self,
        pack: RoundEvidencePack,
        *,
        event_sink: Any | None = None,
        run_id: str | None = None,
        loop_id: str | None = None,
        llm_call_id: str | None = None,
    ) -> Result:
        messages = build_round_judge_messages(pack)
        evaluator_model = str(getattr(self.provider, "model", "unknown"))
        call_id = llm_call_id or f"{pack.llm_call_id}-evaluation-round-judge"
        event_base = {
            "run_id": run_id or pack.run_id,
            "loop_id": loop_id or pack.loop_id,
            "trace_id": pack.trace_id,
            "llm_call_id": call_id,
            "step_number": pack.step_number,
            "model": evaluator_model,
            "source_run_id": pack.run_id,
            "source_loop_id": pack.loop_id,
            "source_llm_call_id": pack.llm_call_id,
        }
        requested = await _emit_event(
            event_sink,
            {
                "type": "llm.requested",
                **event_base,
                "messages": messages,
                "tools": None,
                "at": now_iso(),
            },
        )
        if not requested.ok:
            return requested
        response = await request_llm_response(
            self.provider,
            messages,
            tools=None,
            stream=self.stream,
            emit_event=lambda event: _emit_event(event_sink, event),
            event_metadata=event_base,
            now=now_iso,
        )
        if not response.ok:
            failed = await _emit_event(
                event_sink,
                {
                    "type": "llm.failed",
                    **event_base,
                    "error": response.error,
                    "at": now_iso(),
                },
            )
            if not failed.ok:
                return failed
            return response
        completed = await _emit_event(
            event_sink,
            {
                "type": "llm.completed",
                **event_base,
                "response": response.value,
                "at": now_iso(),
            },
        )
        if not completed.ok:
            return completed
        return parse_round_judge_assessment(
            response.value.content or "",
            pack,
            evaluator_model=evaluator_model,
            token_usage=response.value.usage,
        )


def build_round_evidence_packs(graph: EpisodeGraph, step: StepGraphEpisode) -> tuple[RoundEvidencePack, ...]:
    llm_rounds = tuple(item for item in graph.llm_rounds if item.id in step.llm_round_ids)
    packs: list[RoundEvidencePack] = []
    for index, round_item in enumerate(llm_rounds):
        linked_tools = tuple(tool for tool, _basis in linked_tools_for_round(graph, round_item))
        packs.append(_round_evidence_pack(round_item, index, linked_tools))
    return tuple(packs)


def build_step_evidence_pack(
    graph: EpisodeGraph,
    step: StepGraphEpisode,
    assessment: StepAssessment,
    *,
    round_assessments: tuple[RoundJudgeAssessment, ...] = (),
) -> StepEvidencePack:
    events = _step_events(graph, step)
    llm_rounds = tuple(item for item in graph.llm_rounds if item.id in step.llm_round_ids)
    tool_calls = tuple(item for item in graph.tool_calls if item.id in step.tool_call_ids)
    return StepEvidencePack(
        id=step.id,
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
        round_count=len(llm_rounds),
        tool_call_count=len(tool_calls),
        round_assessments=tuple(_round_judge_summary(item) for item in round_assessments),
        selected_evidence_refs=_selected_evidence_refs(events, assessment, round_assessments),
        evidence_event_hashes=step.event_hashes,
    )


async def _emit_event(event_sink: Any | None, event: Mapping[str, Any]) -> Result:
    if event_sink is None:
        return ok(None)
    emitted = event_sink.emit(event)
    if hasattr(emitted, "__await__"):
        emitted = await emitted
    return emitted


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
    user = LlmMessage(role="user", content=json.dumps(_to_plain(_step_prompt_payload(pack)), ensure_ascii=False, sort_keys=True, indent=2))
    return (system, user)


def build_round_judge_messages(pack: RoundEvidencePack) -> tuple[LlmMessage, LlmMessage]:
    system = LlmMessage(
        role="system",
        content=(
            "You are a round-level evaluator for one LLM round inside an agent loop. Judge only this round's compact evidence. "
            "Return only valid JSON with top-level keys: overall, dimensions, findings, confidence. "
            "Scores must be numbers from 0.0 to 1.0. dimensions must contain exactly these keys: "
            f"{', '.join(ROUND_JUDGE_DIMENSIONS)}. Findings should describe concrete improvement opportunities only."
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
    parsed_payload = _parse_json_object(content)
    if not parsed_payload.ok:
        cause = parsed_payload.error.cause if parsed_payload.error is not None else None
        return err(_parse_error("Could not parse step judge JSON", pack, cause=cause))
    payload = parsed_payload.value
    if not isinstance(payload, Mapping):
        return err(_parse_error("Step judge response must be a JSON object", pack))

    overall = _validate_score(payload.get("overall"), "overall", pack)
    if not overall.ok:
        return overall
    confidence = _validate_score(payload.get("confidence"), "confidence", pack)
    if not confidence.ok:
        return confidence
    dimensions = _validate_dimensions(payload.get("dimensions"), pack, JUDGE_DIMENSIONS)
    if not dimensions.ok:
        return dimensions
    findings = _validate_findings(payload.get("findings", ()), pack)
    if not findings.ok:
        return findings

    finding_items = findings.value
    return ok(
        StepJudgeAssessment(
            id=_subject_id(pack.id, "step", "judge"),
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


def parse_round_judge_assessment(
    content: str,
    pack: RoundEvidencePack,
    *,
    evaluator_model: str,
    token_usage: TokenUsage,
) -> Result:
    parsed_payload = _parse_json_object(content)
    if not parsed_payload.ok:
        cause = parsed_payload.error.cause if parsed_payload.error is not None else None
        return err(_parse_error("Could not parse round judge JSON", pack, cause=cause))
    payload = parsed_payload.value
    if not isinstance(payload, Mapping):
        return err(_parse_error("Round judge response must be a JSON object", pack))

    overall = _validate_score(payload.get("overall"), "overall", pack)
    if not overall.ok:
        return overall
    confidence = _validate_score(payload.get("confidence"), "confidence", pack)
    if not confidence.ok:
        return confidence
    dimensions = _validate_dimensions(payload.get("dimensions"), pack, ROUND_JUDGE_DIMENSIONS)
    if not dimensions.ok:
        return dimensions
    findings = _validate_findings(payload.get("findings", ()), pack)
    if not findings.ok:
        return findings

    finding_items = findings.value
    return ok(
        RoundJudgeAssessment(
            id=_subject_id(pack.id, "llm", "round-judge"),
            run_id=pack.run_id,
            loop_id=pack.loop_id,
            trace_id=pack.trace_id,
            step_number=pack.step_number,
            round_index=pack.round_index,
            round_id=pack.id,
            llm_call_id=pack.llm_call_id,
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


def _parse_json_object(content: str) -> Result:
    try:
        return ok(json.loads(content))
    except json.JSONDecodeError as first_exc:
        decoder = json.JSONDecoder()
        for index, char in enumerate(content):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(content[index:])
                return ok(value)
            except json.JSONDecodeError:
                continue
        return err(make_loom_error("LLM_PARSE_ERROR", "Could not parse JSON object", retryable=True, cause={"message": str(first_exc)}))


def _round_evidence_pack(
    round_item: LlmRoundEpisode,
    round_index: int,
    linked_tools: tuple[ToolCallEpisode, ...],
) -> RoundEvidencePack:
    summary = _llm_round_summary_with_limits(round_item, max_messages=3, message_chars=160)
    return RoundEvidencePack(
        id=round_item.id,
        run_id=round_item.run_id,
        loop_id=round_item.loop_id,
        trace_id=round_item.trace_id,
        step_number=round_item.step_number,
        round_index=round_index,
        llm_call_id=round_item.llm_call_id,
        status=round_item.status,
        model=summary.model,
        finish_reason=summary.finish_reason,
        request_messages=summary.request_messages,
        response_excerpt=summary.response_excerpt,
        tool_call_count=summary.tool_call_count,
        token_total=summary.token_total,
        tool_calls=tuple(_tool_call_summary(item) for item in linked_tools),
        evidence_event_hashes=tuple((*round_item.event_hashes, *(hash_value for item in linked_tools for hash_value in item.event_hashes))),
    )


def _llm_round_summary_with_limits(round_item: LlmRoundEpisode, *, max_messages: int, message_chars: int) -> LlmRoundSummary:
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
        request_messages=_message_excerpts(
            requested.get("messages"),
            max_messages=max_messages,
            max_chars=message_chars,
        ),
        response_excerpt=_excerpt(response.get("content"), max_chars=240),
        tool_call_count=len(tool_calls) if isinstance(tool_calls, list | tuple) else 0,
        token_total=total_tokens_for_events((round_item.completed_event,)) if round_item.completed_event is not None else 0,
        evidence_event_hashes=round_item.event_hashes,
    )


# Backward-compatible helper name used by older tests/imports.
def _llm_round_summary(round_item: LlmRoundEpisode) -> LlmRoundSummary:
    return _llm_round_summary_with_limits(round_item, max_messages=3, message_chars=160)


def _tool_call_summary(tool_call: ToolCallEpisode) -> ToolCallSummary:
    started = tool_call.started_event.payload if tool_call.started_event is not None else {}
    finished_event = tool_call.completed_event or tool_call.failed_event
    output, _field_path = tool_output(finished_event) if finished_event is not None else (None, None)
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


def _message_excerpts(messages: Any, *, max_messages: int = 8, max_chars: int = 240) -> tuple[MessageExcerpt, ...]:
    if not isinstance(messages, list | tuple):
        return ()
    excerpts: list[MessageExcerpt] = []
    for item in messages[-max_messages:]:
        if isinstance(item, Mapping):
            role = str(item.get("role") or "unknown")
            content = item.get("content")
        else:
            role = str(getattr(item, "role", "unknown"))
            content = getattr(item, "content", "")
        excerpts.append(MessageExcerpt(role=role, content_excerpt=_excerpt(content, max_chars=max_chars)))
    return tuple(excerpts)


def _round_judge_summary(assessment: RoundJudgeAssessment) -> RoundJudgeSummary:
    return RoundJudgeSummary(
        round_id=assessment.round_id,
        llm_call_id=assessment.llm_call_id,
        round_index=assessment.round_index,
        status=assessment.status,
        overall=assessment.overall,
        confidence=assessment.confidence,
        dimensions={name: dimension.score for name, dimension in assessment.dimensions.items()},
        findings=tuple(
            {
                "severity": finding.severity,
                "category": finding.category,
                "dimension": finding.dimension,
                "message": finding.message,
                "recommendation": finding.recommendation,
                "confidence": finding.confidence,
                "evidence_event_hash_count": len(finding.evidence_event_hashes),
                "evidence_event_hashes": finding.evidence_event_hashes[:8],
            }
            for finding in assessment.findings
        ),
        evidence_event_hashes=assessment.evidence_event_hashes[:8],
    )


def _selected_evidence_refs(
    events: tuple[NormalizedEvent, ...],
    assessment: StepAssessment,
    round_assessments: tuple[RoundJudgeAssessment, ...],
) -> tuple[EvidenceRef, ...]:
    by_hash = {event.hash: event for event in events if event.hash is not None}
    selected_hashes: list[str] = []
    for finding in assessment.findings:
        selected_hashes.extend(finding.evidence_event_hashes)
    for round_assessment in round_assessments:
        selected_hashes.extend(round_assessment.evidence_event_hashes[:2])
        for finding in round_assessment.findings:
            selected_hashes.extend(finding.evidence_event_hashes)
    if not selected_hashes:
        selected_hashes.extend(event.hash for event in events if event.hash is not None and event.event_type in _REFERENCE_EVENT_TYPES)

    refs: list[EvidenceRef] = []
    seen: set[str] = set()
    for hash_value in selected_hashes:
        if hash_value in seen:
            continue
        event = by_hash.get(hash_value)
        if event is None:
            continue
        if event.event_type not in _REFERENCE_EVENT_TYPES:
            continue
        refs.append(evidence_ref_for_event(event, field_path=_reference_field_path(event)))
        seen.add(hash_value)
        if len(refs) >= 12:
            break
    if not refs:
        for event in events:
            if event.hash is None or event.hash in seen or event.event_type not in _REFERENCE_EVENT_TYPES:
                continue
            refs.append(evidence_ref_for_event(event, field_path=_reference_field_path(event)))
            seen.add(event.hash)
            if len(refs) >= 12:
                break
    return tuple(refs)


def _reference_field_path(event: NormalizedEvent) -> str | None:
    if event.event_type == "llm.completed":
        return "response.content"
    if event.event_type == "tool.started":
        return "input"
    if event.event_type == "tool.completed":
        return "output"
    if event.event_type == "tool.failed":
        return "error"
    if event.event_type == "trace.completed":
        return "outcome"
    return None


_REFERENCE_EVENT_TYPES = frozenset({"llm.completed", "tool.started", "tool.completed", "tool.failed", "trace.completed"})


def _step_prompt_payload(pack: StepEvidencePack) -> Mapping[str, Any]:
    return {
        "run_id": pack.run_id,
        "loop_id": pack.loop_id,
        "trace_id": pack.trace_id,
        "step_number": pack.step_number,
        "step_status": pack.step_status,
        "deterministic_status": pack.deterministic_status,
        "deterministic_score": pack.deterministic_score,
        "deterministic_dimensions": pack.deterministic_dimensions,
        "deterministic_findings": pack.deterministic_findings,
        "token_total": pack.token_total,
        "round_count": pack.round_count,
        "tool_call_count": pack.tool_call_count,
        "round_assessments": pack.round_assessments,
        "selected_evidence_refs": pack.selected_evidence_refs,
        "selected_evidence_event_hashes": tuple(ref.event_hash for ref in pack.selected_evidence_refs if ref.event_hash is not None),
        "evidence_event_hash_count": len(pack.evidence_event_hashes),
    }


def _finding_summary(finding: Finding) -> Mapping[str, Any]:
    return {
        "severity": finding.severity,
        "category": finding.category,
        "dimension": finding.dimension,
        "affected_surface": finding.affected_surface,
        "message": finding.message,
        "evidence_event_hash_count": len(finding.evidence_event_hashes),
        "evidence_event_hashes": finding.evidence_event_hashes[:8],
    }


def _step_events(graph: EpisodeGraph, step: StepGraphEpisode) -> tuple[NormalizedEvent, ...]:
    return tuple(
        event
        for event in graph.events
        if event.run_id == step.run_id
        and event.loop_id == step.loop_id
        and event.trace_id == step.trace_id
        and event.step_number == step.step_number
    )


def _validate_dimensions(value: Any, pack: StepEvidencePack | RoundEvidencePack, dimensions: tuple[str, ...]) -> Result:
    if not isinstance(value, Mapping):
        return err(_parse_error("dimensions must be a JSON object", pack))
    missing = [dimension for dimension in dimensions if dimension not in value]
    if missing:
        return err(_parse_error(f"dimensions missing required keys: {', '.join(missing)}", pack))

    parsed_dimensions: dict[str, JudgeDimensionScore] = {}
    for name in dimensions:
        parsed = _dimension_score(name, value[name], pack)
        if not parsed.ok:
            return parsed
        parsed_dimensions[name] = parsed.value
    return ok(parsed_dimensions)


def _dimension_score(name: str, value: Any, pack: StepEvidencePack | RoundEvidencePack) -> Result:
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


def _validate_findings(value: Any, pack: StepEvidencePack | RoundEvidencePack) -> Result:
    if value is None:
        return ok(())
    if isinstance(value, Mapping):
        value = (value,)
    if not isinstance(value, list | tuple):
        return err(_parse_error("findings must be an array", pack))
    findings: list[JudgeFinding] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            if item is None:
                continue
            item = {"message": str(item)}
        confidence = _validate_score(item.get("confidence", 0.5), f"findings[{index}].confidence", pack)
        if not confidence.ok:
            return confidence
        hashes = _string_tuple(item.get("evidence_event_hashes", ()), f"findings[{index}].evidence_event_hashes", pack)
        if not hashes.ok:
            return hashes
        category = str(item.get("category") or "judge_finding")
        findings.append(
            JudgeFinding(
                id=f"{_judge_finding_subject_id(pack)}:{index}:{category}",
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


def _validate_score(value: Any, name: str, pack: StepEvidencePack | RoundEvidencePack) -> Result:
    if value is None:
        return err(_parse_error(f"{name} is required", pack))
    try:
        score = float(value)
    except (TypeError, ValueError):
        return err(_parse_error(f"{name} must be a number from 0.0 to 1.0", pack))
    if score < 0.0 or score > 1.0:
        return err(_parse_error(f"{name} must be a number from 0.0 to 1.0", pack))
    return ok(score)


def _judge_finding_subject_id(pack: StepEvidencePack | RoundEvidencePack) -> str:
    source_prefix = "llm" if isinstance(pack, RoundEvidencePack) else "step"
    return _subject_id(pack.id, source_prefix, "judge-finding")


def _subject_id(subject_id: str, source_prefix: str, target_prefix: str) -> str:
    return f"{target_prefix}:{subject_id.removeprefix(f'{source_prefix}:')}"


def _string_tuple(value: Any, name: str, pack: StepEvidencePack | RoundEvidencePack) -> Result:
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


def _parse_error(message: str, pack: StepEvidencePack | RoundEvidencePack, *, cause: Mapping[str, Any] | None = None) -> Any:
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
    "ROUND_JUDGE_DIMENSIONS",
    "JudgeDimensionScore",
    "JudgeFinding",
    "LlmRoundSummary",
    "LlmRoundJudge",
    "LlmStepJudge",
    "MessageExcerpt",
    "RoundEvidencePack",
    "RoundJudgeAssessment",
    "RoundJudgeSummary",
    "StepEvidencePack",
    "StepJudgeAssessment",
    "ToolCallSummary",
    "build_round_evidence_packs",
    "build_round_judge_messages",
    "build_step_evidence_pack",
    "build_step_judge_messages",
    "parse_round_judge_assessment",
    "parse_step_judge_assessment",
]
