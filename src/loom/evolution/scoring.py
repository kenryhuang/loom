"""LLM scoring for trace evolution step episodes."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, now_iso, ok, thaw_json
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

    async def score(
        self,
        episode: StepEpisode,
        *,
        event_sink: Any | None = None,
        run_id: str | None = None,
        loop_id: str | None = None,
        llm_call_id: str | None = None,
    ) -> Result:
        messages = build_step_scoring_messages(episode)
        evaluator_model = str(getattr(self.provider, "model", "unknown"))
        call_id = llm_call_id or f"{episode.trace_id}-evolution-score-llm"
        event_base = {
            "run_id": run_id or episode.run_id,
            "loop_id": loop_id or episode.loop_id,
            "trace_id": episode.trace_id,
            "llm_call_id": call_id,
            "step_number": episode.step_number,
            "model": evaluator_model,
            "source_run_id": episode.run_id,
            "source_loop_id": episode.loop_id,
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

        response = await self.provider.chat(messages, tools=None)
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

        return parse_step_score(
            response.value.content or "",
            episode,
            evaluator_model=evaluator_model,
            token_usage=response.value.usage,
        )


async def _emit_event(event_sink: Any | None, event: Mapping[str, Any]) -> Result:
    if event_sink is None:
        return ok(None)
    emitted = event_sink.emit(event)
    if hasattr(emitted, "__await__"):
        emitted = await emitted
    return emitted


def build_step_scoring_messages(episode: StepEpisode) -> tuple[LlmMessage, LlmMessage]:
    system = LlmMessage(
        role="system",
        content=(
            "You are a step evolution judge. Score one Loom execution step from trace evidence. "
            "Return only valid JSON. Use decimal scores from 0.0 to 1.0 only, never 1-5 scores or percentages. "
            "Only include attribution for problems or concrete improvement opportunities; do not include praise, "
            "successful behavior, or neutral observations in attribution. If the step has no meaningful issue, "
            "return attribution as an empty object and proposed_fixes as an empty array. "
            "The JSON object must contain exactly these top-level keys: overall, dimensions, attribution, "
            "proposed_fixes, and confidence. dimensions must contain exactly these keys: "
            f"{', '.join(SCORE_DIMENSIONS)}. attribution must be an object whose values are arrays of strings. "
            "Prefer attribution keys that name mutable surfaces, such as system_prompt, user_prompt, tool_schema, "
            "tool_description, tool_collection, skill_context, context_policy, loop_control, or observability."
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

    normalized = _normalize_dimension_aliases(value)
    missing = [dimension for dimension in SCORE_DIMENSIONS if dimension not in normalized]
    if missing:
        return err(_parse_error(f"dimensions missing required keys: {', '.join(missing)}", episode))

    dimensions: dict[str, float] = {}
    five_point_scale = _uses_five_point_scale(tuple(normalized[dimension] for dimension in SCORE_DIMENSIONS))
    for dimension in SCORE_DIMENSIONS:
        score = _validate_score_number(normalized[dimension], f"dimensions.{dimension}", episode, five_point_scale=five_point_scale)
        if not score.ok:
            return score
        dimensions[dimension] = score.value
    return ok(dimensions)


def _normalize_dimension_aliases(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {str(key): item for key, item in value.items()}
    aliases = {
        "tool_usage": ("tool_choice_quality", "tool_argument_quality"),
        "tool_use": ("tool_choice_quality", "tool_argument_quality"),
        "tool_quality": ("tool_choice_quality", "tool_argument_quality"),
        "instruction_following": ("prompt_following", "context_relevance"),
        "format_following": ("prompt_following",),
        "reasoning": ("task_progress", "evidence_grounding", "failure_recovery"),
        "reasoning_quality": ("task_progress", "evidence_grounding", "failure_recovery"),
        "efficiency": ("cost_efficiency",),
        "cost": ("cost_efficiency",),
    }
    for alias, targets in aliases.items():
        if alias not in normalized:
            continue
        for target in targets:
            normalized.setdefault(target, normalized[alias])
    return normalized


def _validate_attribution(value: Any, episode: StepEpisode) -> Result:
    if isinstance(value, str):
        return ok({"general": (value,)})
    if isinstance(value, list | tuple):
        items = _validate_string_tuple(value, "attribution", episode)
        if not items.ok:
            return items
        return ok({"general": items.value})
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


def _validate_score_number(value: Any, path: str, episode: StepEpisode, *, five_point_scale: bool = False) -> Result:
    score = _coerce_score_number(value, five_point_scale=five_point_scale)
    if score is None:
        return err(_parse_error(f"{path} must be a number from 0.0 to 1.0", episode))
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return err(_parse_error(f"{path} must be a finite number from 0.0 to 1.0", episode))
    return ok(score)


def _coerce_score_number(value: Any, *, five_point_scale: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        score = float(value)
        if five_point_scale and 1.0 <= score <= 5.0:
            return score / 5
        if 0.0 <= score <= 1.0:
            return score
        if isinstance(value, int) and 1 < value <= 5:
            return value / 5
        if isinstance(value, int) and 5 < value <= 100:
            return value / 100
        return score
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith("%"):
        return _parse_percentage_score(text[:-1])
    if "/" in text:
        return _parse_fraction_score(text)
    try:
        parsed = float(text)
    except ValueError:
        return None
    if five_point_scale and 1.0 <= parsed <= 5.0:
        return parsed / 5
    if 0.0 <= parsed <= 1.0:
        return parsed
    if parsed.is_integer() and 1 < parsed <= 5:
        return parsed / 5
    if parsed.is_integer() and 5 < parsed <= 100:
        return parsed / 100
    return parsed


def _uses_five_point_scale(values: tuple[Any, ...]) -> bool:
    for value in values:
        parsed = _numeric_score_value(value)
        if parsed is not None and 1.0 < parsed <= 5.0:
            return True
    return False


def _numeric_score_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("%") or "/" in text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_percentage_score(text: str) -> float | None:
    try:
        parsed = float(text.strip())
    except ValueError:
        return None
    if 0.0 <= parsed <= 100.0:
        return parsed / 100
    return parsed


def _parse_fraction_score(text: str) -> float | None:
    numerator_text, separator, denominator_text = text.partition("/")
    if not separator:
        return None
    try:
        numerator = float(numerator_text.strip())
        denominator = float(denominator_text.strip())
    except ValueError:
        return None
    if denominator <= 0:
        return None
    return numerator / denominator


def _parse_error(message: str, episode: StepEpisode) -> Any:
    return make_loom_error("LLM_PARSE_ERROR", message, retryable=True, trace_id=episode.trace_id)


__all__ = [
    "LlmStepScorer",
    "SCORE_DIMENSIONS",
    "StepScore",
    "build_step_scoring_messages",
    "parse_step_score",
]
