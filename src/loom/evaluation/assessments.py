"""Step-level assessment for Loom evaluation episode graphs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from loom.core import JsonValue
from loom.evaluation.episodes import EpisodeGraph, StepGraphEpisode, ToolCallEpisode
from loom.evaluation.records import NormalizedEvent
from loom.evaluation.token_usage import total_tokens_for_events

DIMENSIONS = (
    "step_completion",
    "llm_response",
    "tool_execution",
    "tool_result_handling",
    "efficiency",
    "observability",
)


@dataclass(frozen=True, slots=True)
class DimensionScore:
    name: str
    score: float
    status: str
    rationale: str
    evidence_event_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    severity: str
    category: str
    message: str
    dimension: str
    affected_surface: str
    evidence_event_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


@dataclass(frozen=True, slots=True)
class StepAssessment:
    id: str
    run_id: str
    loop_id: str
    trace_id: str
    step_number: int
    status: str
    aggregate_score: float
    dimensions: Mapping[str, DimensionScore]
    findings: tuple[Finding, ...]
    evidence_event_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", dict(self.dimensions))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "evidence_event_hashes", tuple(self.evidence_event_hashes))


def assess_steps(graph: EpisodeGraph) -> tuple[StepAssessment, ...]:
    return tuple(_assess_step(graph, step) for step in graph.steps)


def _assess_step(graph: EpisodeGraph, step: StepGraphEpisode) -> StepAssessment:
    step_events = _step_events(graph, step)
    llm_rounds = tuple(round_item for round_item in graph.llm_rounds if round_item.id in step.llm_round_ids)
    tool_calls = tuple(tool_item for tool_item in graph.tool_calls if tool_item.id in step.tool_call_ids)
    findings: list[Finding] = []
    dimensions = _perfect_dimensions(step.event_hashes)

    if step.status != "complete":
        hashes = _hashes(step_events) or step.event_hashes
        findings.append(
            _finding(
                step,
                "error",
                "step_incomplete",
                "Step did not contain both step completion and trace completion evidence.",
                "step_completion",
                "loop_control",
                hashes,
            )
        )
        dimensions["step_completion"] = _dimension("step_completion", 0.0, "fail", "Step episode is incomplete.", hashes)

    missing_llm = tuple(item for item in llm_rounds if item.status != "complete")
    if missing_llm:
        hashes = _hashes_for_ids(graph, [event_hash for item in missing_llm for event_hash in item.event_hashes])
        findings.append(
            _finding(
                step,
                "error",
                "llm_round_incomplete",
                "LLM round did not complete cleanly.",
                "llm_response",
                "llm_provider",
                hashes,
            )
        )
        dimensions["llm_response"] = _dimension("llm_response", 0.25, "fail", "At least one LLM round is incomplete or failed.", hashes)

    failed_tools = tuple(item for item in tool_calls if item.status == "failed")
    partial_tools = tuple(item for item in tool_calls if item.status == "partial")
    if failed_tools:
        hashes = tuple(hash_value for item in failed_tools for hash_value in item.event_hashes)
        findings.append(
            _finding(
                step,
                "error",
                "tool_failure",
                "One or more tool calls failed.",
                "tool_execution",
                "tool_call",
                hashes,
            )
        )
        dimensions["tool_execution"] = _dimension("tool_execution", 0.0, "fail", "At least one tool call failed.", hashes)
    elif partial_tools:
        hashes = tuple(hash_value for item in partial_tools for hash_value in item.event_hashes)
        findings.append(
            _finding(
                step,
                "warning",
                "tool_call_incomplete",
                "One or more tool calls did not complete.",
                "tool_execution",
                "tool_call",
                hashes,
            )
        )
        dimensions["tool_execution"] = _dimension("tool_execution", 0.4, "warn", "At least one tool call is partial.", hashes)

    result_handling = _assess_tool_result_handling(graph, step, tool_calls)
    if result_handling is not None:
        finding, dimension = result_handling
        findings.append(finding)
        dimensions["tool_result_handling"] = dimension

    token_total = _step_token_total(step_events)
    if token_total > 32000:
        hashes = _hashes(step_events)
        findings.append(
            _finding(
                step,
                "warning",
                "high_token_usage",
                f"Step used {token_total} tokens, which is above the deterministic warning threshold.",
                "efficiency",
                "context_policy",
                hashes,
            )
        )
        dimensions["efficiency"] = _dimension("efficiency", 0.5, "warn", "Token usage exceeded deterministic threshold.", hashes)

    if not _hashes(step_events):
        findings.append(
            _finding(
                step,
                "warning",
                "missing_event_hashes",
                "Step events do not include stable evidence hashes.",
                "observability",
                "observability",
                (),
            )
        )
        dimensions["observability"] = _dimension("observability", 0.5, "warn", "Step lacks event hashes for evidence attribution.", ())

    aggregate = _aggregate_score(dimensions)
    return StepAssessment(
        id=f"assessment:{step.run_id}:{step.trace_id}:{step.step_number}",
        run_id=step.run_id,
        loop_id=step.loop_id,
        trace_id=step.trace_id,
        step_number=step.step_number,
        status=_status(findings),
        aggregate_score=aggregate,
        dimensions=dimensions,
        findings=tuple(findings),
        evidence_event_hashes=step.event_hashes,
    )


def _perfect_dimensions(hashes: tuple[str, ...]) -> dict[str, DimensionScore]:
    return {name: _dimension(name, 1.0, "pass", "No deterministic issue detected.", hashes) for name in DIMENSIONS}


def _assess_tool_result_handling(
    graph: EpisodeGraph,
    step: StepGraphEpisode,
    tool_calls: tuple[ToolCallEpisode, ...],
) -> tuple[Finding, DimensionScore] | None:
    completed_tools = tuple(item for item in tool_calls if item.status == "complete")
    if not completed_tools:
        return None
    events = _step_events(graph, step)
    last_tool_index = _last_index(events, lambda event: event.event_type == "tool.completed")
    last_llm_index = _last_index(events, lambda event: event.event_type == "llm.completed")
    if last_tool_index is None or last_llm_index is None or last_llm_index > last_tool_index:
        return None
    hashes = tuple(hash_value for item in completed_tools for hash_value in item.event_hashes)
    return (
        _finding(
            step,
            "warning",
            "tool_result_not_followed_by_llm",
            "Tool result was not followed by a completed LLM response in the same step.",
            "tool_result_handling",
            "loop_control",
            hashes,
        ),
        _dimension("tool_result_handling", 0.6, "warn", "Completed tool output was not followed by an LLM completion.", hashes),
    )


def _step_events(graph: EpisodeGraph, step: StepGraphEpisode) -> tuple[NormalizedEvent, ...]:
    return tuple(event for event in graph.events if event.run_id == step.run_id and event.trace_id == step.trace_id and event.step_number == step.step_number)


def _hashes(events: tuple[NormalizedEvent, ...]) -> tuple[str, ...]:
    return tuple(event.hash for event in events if event.hash is not None)


def _hashes_for_ids(_graph: EpisodeGraph, hashes: list[str]) -> tuple[str, ...]:
    return tuple(hashes)


def _last_index(events: tuple[NormalizedEvent, ...], predicate) -> int | None:
    for index in range(len(events) - 1, -1, -1):
        if predicate(events[index]):
            return index
    return None


def _step_token_total(events: tuple[NormalizedEvent, ...]) -> int:
    return total_tokens_for_events(events)


def _dimension(name: str, score: float, status: str, rationale: str, hashes: tuple[str, ...]) -> DimensionScore:
    return DimensionScore(name=name, score=score, status=status, rationale=rationale, evidence_event_hashes=hashes)


def _finding(
    step: StepGraphEpisode,
    severity: str,
    category: str,
    message: str,
    dimension: str,
    affected_surface: str,
    hashes: tuple[str, ...],
) -> Finding:
    return Finding(
        id=f"finding:{step.run_id}:{step.trace_id}:{step.step_number}:{category}",
        severity=severity,
        category=category,
        message=message,
        dimension=dimension,
        affected_surface=affected_surface,
        evidence_event_hashes=hashes,
    )


def _aggregate_score(dimensions: Mapping[str, DimensionScore]) -> float:
    if not dimensions:
        return 0.0
    return min(1.0, max(0.0, sum(item.score for item in dimensions.values()) / len(dimensions)))


def _status(findings: tuple[Finding, ...] | list[Finding]) -> str:
    if any(item.severity == "error" for item in findings):
        return "fail"
    if findings:
        return "warn"
    return "pass"


def assessment_to_metric_value(assessment: StepAssessment) -> JsonValue:
    return {
        "status": assessment.status,
        "aggregate_score": assessment.aggregate_score,
        "dimensions": {key: value.score for key, value in assessment.dimensions.items()},
        "finding_count": len(assessment.findings),
    }


__all__ = [
    "DIMENSIONS",
    "DimensionScore",
    "Finding",
    "StepAssessment",
    "assess_steps",
    "assessment_to_metric_value",
]
