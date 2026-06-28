"""LLM scoring for trace evolution step episodes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.evolution.episodes import StepEpisode
from loom.llm import LlmMessage, TokenUsage

SCORE_DIMENSIONS = (
    "task_progress",
    "evidence_grounding",
    "tool_choice_quality",
    "tool_argument_quality",
    "context_relevance",
    "prompt_following",
    "cost_efficiency",
    "failure_recovery",
)


@dataclass(frozen=True, slots=True)
class StepScore:
    run_id: str
    trace_id: str
    step_number: int
    overall: float
    dimensions: Mapping[str, float]
    attribution: Mapping[str, tuple[str, ...]]
    proposed_fixes: tuple[str, ...]
    evidence_event_hashes: tuple[str, ...]
    confidence: float
    evaluator_model: str
    token_usage: TokenUsage


class LlmStepScorer:
    def __init__(self, provider: Any):
        self.provider = provider

    async def score(self, episode: StepEpisode) -> Result:
        response = await self.provider.chat(build_step_scoring_messages(episode), tools=None)
        if not response.ok:
            return response
        evaluator_model = str(getattr(self.provider, "model", "unknown"))
        return parse_step_score(
            response.value.content or "",
            episode,
            evaluator_model=evaluator_model,
            token_usage=response.value.usage,
        )


def build_step_scoring_messages(episode: StepEpisode) -> tuple[LlmMessage, LlmMessage]:
    system = LlmMessage(
        role="system",
        content=(
            "You are a step evolution judge. Score one Loom execution step from trace evidence. "
            "Return only valid JSON with overall, dimensions, attribution, proposed_fixes, and confidence."
        ),
    )
    user = LlmMessage(
        role="user",
        content=json.dumps(_episode_evidence(episode), sort_keys=True, indent=2),
    )
    return (system, user)


def parse_step_score(
    content: str,
    episode: StepEpisode,
    *,
    evaluator_model: str,
    token_usage: TokenUsage,
) -> Result:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        return err(
            make_loom_error(
                "LLM_PARSE_ERROR",
                "Could not parse step score JSON",
                retryable=True,
                trace_id=episode.trace_id,
                cause={"message": str(exc)},
            )
        )

    if not isinstance(payload, Mapping):
        return err(_parse_error("Step score response must be a JSON object", episode))

    return ok(
        StepScore(
            run_id=episode.run_id,
            trace_id=episode.trace_id,
            step_number=episode.step_number,
            overall=_float_field(payload, "overall"),
            dimensions=_score_dimensions(payload.get("dimensions")),
            attribution=_string_tuple_mapping(payload.get("attribution")),
            proposed_fixes=_string_tuple(payload.get("proposed_fixes")),
            evidence_event_hashes=episode.event_hashes,
            confidence=_float_field(payload, "confidence"),
            evaluator_model=evaluator_model,
            token_usage=token_usage,
        )
    )


def _episode_evidence(episode: StepEpisode) -> Mapping[str, Any]:
    return {
        "run_id": episode.run_id,
        "trace_id": episode.trace_id,
        "loop_id": episode.loop_id,
        "step_number": episode.step_number,
        "started_event": episode.started_event,
        "llm_requests": episode.llm_requests,
        "llm_completions": episode.llm_completions,
        "tool_events": episode.tool_events,
        "action_events": episode.action_events,
        "observation_events": episode.observation_events,
        "completed_trace": episode.completed_trace,
        "completed_event": episode.completed_event,
        "event_hashes": episode.event_hashes,
    }


def _score_dimensions(value: Any) -> Mapping[str, float]:
    source = value if isinstance(value, Mapping) else {}
    return {dimension: _to_float(source.get(dimension, 0.0)) for dimension in SCORE_DIMENSIONS}


def _string_tuple_mapping(value: Any) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _string_tuple(items) for key, items in value.items()}


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _float_field(payload: Mapping[str, Any], key: str) -> float:
    return _to_float(payload.get(key, 0.0))


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_error(message: str, episode: StepEpisode) -> Any:
    return make_loom_error("LLM_PARSE_ERROR", message, retryable=True, trace_id=episode.trace_id)


__all__ = [
    "LlmStepScorer",
    "SCORE_DIMENSIONS",
    "StepScore",
    "build_step_scoring_messages",
    "parse_step_score",
]
