import asyncio
import json

from loom.core import freeze_json, ok
from loom.evolution.episodes import StepEpisode
from loom.evolution.scoring import (
    SCORE_DIMENSIONS,
    LlmStepScorer,
    StepScore,
    build_step_scoring_messages,
    parse_step_score,
)
from loom.llm import LlmResponse, TokenUsage


class FakeScoreProvider:
    model = "fake-score-model"

    def __init__(self, content):
        self.content = content
        self.messages = None

    async def chat(self, messages, tools=None, cancellation=None):
        self.messages = messages
        return ok(LlmResponse(content=self.content, usage=TokenUsage(11, 7, 18)))


def _episode():
    return StepEpisode(
        run_id="run-1",
        trace_id="trace-1",
        loop_id="loop-1",
        step_number=0,
        started_event={"type": "step.started"},
        llm_requests=({"type": "llm.requested", "messages": [{"role": "user", "content": "audit"}]},),
        llm_completions=({"type": "llm.completed", "response": {"content": "bad json"}},),
        tool_events=({"type": "tool.completed", "tool_id": "inspect-project", "input": {}, "output": {"ok": True}},),
        action_events=(),
        observation_events=(),
        completed_trace={"id": "trace-1", "outcome": "pass", "metadata": {"tokenUsage": {"totalTokens": 42}}},
        completed_event={"type": "step.completed"},
        event_hashes=("hash-1",),
    )


def _score_json():
    return json.dumps(
        {
            "overall": 0.62,
            "dimensions": {
                "task_progress": 0.7,
                "evidence_grounding": 0.5,
                "tool_choice_quality": 0.8,
                "tool_argument_quality": 0.9,
                "context_relevance": 0.6,
                "prompt_following": 0.4,
                "cost_efficiency": 0.7,
                "failure_recovery": 0.3,
            },
            "attribution": {"system_prompt": ["Output format was too easy to violate."], "tool_schema": []},
            "proposed_fixes": ["Clarify required JSON output shape."],
            "confidence": 0.81,
        }
    )


def test_build_step_scoring_messages_contains_episode_evidence():
    messages = build_step_scoring_messages(_episode())

    assert messages[0].role == "system"
    assert "step evolution judge" in messages[0].content.lower()
    assert "trace-1" in messages[1].content
    assert "inspect-project" in messages[1].content
    assert "tokenUsage" in messages[1].content


def test_build_step_scoring_messages_handles_non_plain_values():
    episode = StepEpisode(
        run_id="run-1",
        trace_id="trace-1",
        loop_id="loop-1",
        step_number=0,
        started_event=freeze_json({"type": "step.started", "metadata": {"tags": ["scoring"]}}),
        llm_requests=(
            freeze_json({"type": "llm.requested", "messages": [{"role": "user", "content": "audit"}]}),
        ),
        llm_completions=(),
        tool_events=(),
        action_events=(),
        observation_events=(),
        completed_trace=freeze_json({"id": "trace-1", "metadata": {"tokenUsage": {"totalTokens": 42}}}),
        completed_event=freeze_json({"type": "step.completed"}),
        event_hashes=("hash-1",),
    )

    messages = build_step_scoring_messages(episode)

    evidence = json.loads(messages[1].content)
    assert evidence["started_event"]["metadata"]["tags"] == ["scoring"]
    assert evidence["completed_trace"]["metadata"]["tokenUsage"]["totalTokens"] == 42


def test_parse_step_score_returns_structured_score():
    score = parse_step_score(
        _score_json(),
        _episode(),
        evaluator_model="fake-score-model",
        token_usage=TokenUsage(1, 2, 3),
    ).unwrap()

    assert isinstance(score, StepScore)
    assert score.trace_id == "trace-1"
    assert score.overall == 0.62
    assert score.dimensions["prompt_following"] == 0.4
    assert score.attribution["system_prompt"] == ("Output format was too easy to violate.",)
    assert score.proposed_fixes == ("Clarify required JSON output shape.",)
    assert score.confidence == 0.81
    assert score.token_usage.total_tokens == 3


def test_parse_step_score_rejects_invalid_json():
    result = parse_step_score("not-json", _episode(), evaluator_model="fake-score-model", token_usage=TokenUsage())

    assert not result.ok
    assert result.error.code == "LLM_PARSE_ERROR"


def test_parse_step_score_rejects_missing_required_keys():
    payload = json.loads(_score_json())
    del payload["confidence"]

    result = parse_step_score(json.dumps(payload), _episode(), evaluator_model="fake-score-model", token_usage=TokenUsage())

    assert not result.ok
    assert result.error.code == "LLM_PARSE_ERROR"


def test_parse_step_score_rejects_out_of_range_or_non_numeric_scores():
    cases = [
        {"overall": 1.1},
        {"confidence": "high"},
        {"dimensions": {**{dimension: 0.5 for dimension in SCORE_DIMENSIONS}, "prompt_following": -0.1}},
        {"dimensions": {**{dimension: 0.5 for dimension in SCORE_DIMENSIONS}, "cost_efficiency": "cheap"}},
    ]

    for override in cases:
        payload = json.loads(_score_json())
        payload.update(override)

        result = parse_step_score(
            json.dumps(payload),
            _episode(),
            evaluator_model="fake-score-model",
            token_usage=TokenUsage(),
        )

        assert not result.ok
        assert result.error.code == "LLM_PARSE_ERROR"


def test_parse_step_score_rejects_malformed_attribution_and_fixes():
    cases = [
        {"attribution": ["system_prompt"]},
        {"attribution": {"system_prompt": "bad"}},
        {"attribution": {"system_prompt": [7]}},
        {"proposed_fixes": "Clarify JSON."},
        {"proposed_fixes": ["Clarify JSON.", 7]},
    ]

    for override in cases:
        payload = json.loads(_score_json())
        payload.update(override)

        result = parse_step_score(
            json.dumps(payload),
            _episode(),
            evaluator_model="fake-score-model",
            token_usage=TokenUsage(),
        )

        assert not result.ok
        assert result.error.code == "LLM_PARSE_ERROR"


def test_llm_step_scorer_calls_provider_and_parses_score():
    async def scenario():
        provider = FakeScoreProvider(_score_json())
        scorer = LlmStepScorer(provider)

        result = await scorer.score(_episode())

        assert result.ok
        assert result.value.trace_id == "trace-1"
        assert provider.messages is not None

    asyncio.run(scenario())
