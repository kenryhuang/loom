"""LLM scoring for trace evolution step episodes."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, ok, thaw_json
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
        content=json.dumps(_to_plain(_episode_evidence(episode)), sort_keys=True, indent=2),
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

    validated = _validate_score_payload(payload, episode)
    if not validated.ok:
        return validated
    overall, dimensions, attribution, proposed_fixes, confidence = validated.value

    return ok(
        StepScore(
            run_id=episode.run_id,
            trace_id=episode.trace_id,
            step_number=episode.step_number,
            overall=overall,
            dimensions=dimensions,
            attribution=attribution,
            proposed_fixes=proposed_fixes,
            evidence_event_hashes=episode.event_hashes,
            confidence=confidence,
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


def _to_plain(value: Any) -> Any:
    value = thaw_json(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    return str(value)


def _validate_score_payload(payload: Mapping[str, Any], episode: StepEpisode) -> Result:
    required_keys = ("overall", "dimensions", "attribution", "proposed_fixes", "confidence")
    missing = [key for key in required_keys if key not in payload]
    if missing:
        return err(_parse_error(f"Step score response missing required keys: {', '.join(missing)}", episode))

    overall = _validate_score_number(payload["overall"], "overall", episode)
    if not overall.ok:
        return overall

    confidence = _validate_score_number(payload["confidence"], "confidence", episode)
    if not confidence.ok:
        return confidence

    dimensions = _validate_dimensions(payload["dimensions"], episode)
    if not dimensions.ok:
        return dimensions

    attribution = _validate_attribution(payload["attribution"], episode)
    if not attribution.ok:
        return attribution

    proposed_fixes = _validate_string_tuple(payload["proposed_fixes"], "proposed_fixes", episode)
    if not proposed_fixes.ok:
        return proposed_fixes

    return ok((overall.value, dimensions.value, attribution.value, proposed_fixes.value, confidence.value))


def _validate_dimensions(value: Any, episode: StepEpisode) -> Result:
    if not isinstance(value, Mapping):
        return err(_parse_error("dimensions must be a JSON object", episode))

    missing = [dimension for dimension in SCORE_DIMENSIONS if dimension not in value]
    if missing:
        return err(_parse_error(f"dimensions missing required keys: {', '.join(missing)}", episode))

    dimensions: dict[str, float] = {}
    for dimension in SCORE_DIMENSIONS:
        score = _validate_score_number(value[dimension], f"dimensions.{dimension}", episode)
        if not score.ok:
            return score
        dimensions[dimension] = score.value
    return ok(dimensions)


def _validate_attribution(value: Any, episode: StepEpisode) -> Result:
    if not isinstance(value, Mapping):
        return err(_parse_error("attribution must be a JSON object", episode))

    attribution: dict[str, tuple[str, ...]] = {}
    for key, items in value.items():
        result = _validate_string_tuple(items, f"attribution.{key}", episode)
        if not result.ok:
            return result
        attribution[str(key)] = result.value
    return ok(attribution)


def _validate_string_tuple(value: Any, path: str, episode: StepEpisode) -> Result:
    if not isinstance(value, list | tuple):
        return err(_parse_error(f"{path} must be a list of strings", episode))
    if not all(isinstance(item, str) for item in value):
        return err(_parse_error(f"{path} must contain only strings", episode))
    return ok(tuple(value))


def _validate_score_number(value: Any, path: str, episode: StepEpisode) -> Result:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return err(_parse_error(f"{path} must be a number from 0.0 to 1.0", episode))
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return err(_parse_error(f"{path} must be a finite number from 0.0 to 1.0", episode))
    return ok(score)


def _parse_error(message: str, episode: StepEpisode) -> Any:
    return make_loom_error("LLM_PARSE_ERROR", message, retryable=True, trace_id=episode.trace_id)


__all__ = [
    "LlmStepScorer",
    "SCORE_DIMENSIONS",
    "StepScore",
    "build_step_scoring_messages",
    "parse_step_score",
]
