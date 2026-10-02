import asyncio
import json

from loom.core import ok
from loom.evaluation.assessments import assess_steps
from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.judge import (
    JUDGE_DIMENSIONS,
    ROUND_JUDGE_DIMENSIONS,
    LlmRoundJudge,
    LlmStepJudge,
    RoundJudgeAssessment,
    StepJudgeAssessment,
    build_round_evidence_packs,
    build_round_judge_messages,
    build_step_evidence_pack,
    build_step_judge_messages,
    parse_round_judge_assessment,
    parse_step_judge_assessment,
)
from loom.evaluation.records import NormalizedEvent
from loom.llm import LlmResponse, TokenUsage


class FakeJudgeProvider:
    model = "fake-judge-model"

    def __init__(self, content):
        self.content = content
        self.messages = None

    async def chat(self, messages, tools=None, cancellation=None):
        self.messages = messages
        return ok(LlmResponse(content=self.content, usage=TokenUsage(3, 4, 7)))


def _event(
    event_type,
    *,
    hash,
    run_id="run-1",
    loop_id="loop-1",
    trace_id="trace-1",
    step_number=0,
    llm_call_id=None,
    tool_call_id=None,
    tool_id=None,
    payload=None,
):
    event_payload = {"type": event_type, "run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step_number}
    if payload:
        event_payload.update(payload)
    return NormalizedEvent(
        record_id=hash,
        event_type=event_type,
        run_id=run_id,
        loop_id=loop_id,
        trace_id=trace_id,
        step_number=step_number,
        llm_call_id=llm_call_id,
        tool_call_id=tool_call_id,
        tool_id=tool_id,
        at=None,
        payload=event_payload,
        hash=hash,
    )


def _graph():
    return build_episode_graph(
        (
            _event("step.started", hash="step-start"),
            _event(
                "llm.requested",
                llm_call_id="llm-1",
                payload={
                    "messages": [
                        {"role": "system", "content": "old context " + ("x" * 1000)},
                        {"role": "assistant", "content": "previous response"},
                        {"role": "tool", "content": "previous tool result"},
                        {"role": "user", "content": "Audit the project briefly."},
                    ]
                },
                hash="llm-request",
            ),
            _event(
                "llm.completed",
                llm_call_id="llm-1",
                payload={
                    "response": {
                        "content": "I will inspect files first.",
                        "finish_reason": "tool_calls",
                        "tool_calls": [{"id": "call-1", "name": "read_file", "arguments": '{"path":"README.md"}'}],
                        "usage": {"total_tokens": 123},
                    }
                },
                hash="llm-complete",
            ),
            _event("tool.started", tool_call_id="call-1", tool_id="read_file", payload={"input": {"path": "README.md"}}, hash="tool-start"),
            _event(
                "tool.completed",
                tool_call_id="call-1",
                tool_id="read_file",
                payload={
                    "output": {
                        "content": (
                            "YakDB turns files into searchable text. "
                            "This README excerpt is intentionally long so the evidence pack keeps only a bounded summary."
                        )
                    }
                },
                hash="tool-complete",
            ),
            _event("llm.requested", llm_call_id="llm-2", hash="llm-request-2"),
            _event(
                "llm.completed",
                llm_call_id="llm-2",
                payload={"response": {"content": "Final audit.", "usage": {"total_tokens": 50}}},
                hash="llm-complete-2",
            ),
            _event("step.completed", hash="step-complete"),
            _event("trace.completed", payload={"metadata": {"tokenUsage": {"totalTokens": 173}}, "outcome": "pass"}, hash="trace-complete"),
        )
    )


def _judge_json():
    return json.dumps(
        {
            "overall": 0.72,
            "dimensions": {
                "task_progress": {"score": 0.8, "rationale": "The step made progress."},
                "instruction_following": {"score": 0.7, "rationale": "The requested audit was addressed."},
                "tool_selection": {"score": 0.9, "rationale": "Reading README was appropriate."},
                "tool_arguments": {"score": 0.95, "rationale": "Arguments were valid."},
                "tool_result_handling": {"score": 0.65, "rationale": "Tool output was used, but lightly."},
                "evidence_grounding": {"score": 0.7, "rationale": "Claims cite observed README content."},
                "context_quality": {"score": 0.6, "rationale": "Context was sufficient."},
                "efficiency": {"score": 0.55, "rationale": "Extra round was acceptable."},
                "recovery": {"score": 1.0, "rationale": "No recovery needed."},
            },
            "findings": [
                {
                    "severity": "warning",
                    "category": "weak_tool_result_handling",
                    "dimension": "tool_result_handling",
                    "affected_surface": "loop_control",
                    "message": "The response used the README output only shallowly.",
                    "recommendation": "Ask the model to cite specific observed files in final audits.",
                    "confidence": 0.77,
                    "evidence_event_hashes": ["tool-complete", "llm-complete-2"],
                }
            ],
            "confidence": 0.82,
        }
    )


def _round_judge_json():
    return json.dumps(
        {
            "overall": 0.64,
            "dimensions": {
                "round_progress": {"score": 0.7, "rationale": "The round made useful progress."},
                "instruction_following": {"score": 0.7, "rationale": "The round followed the task."},
                "tool_selection": {"score": 0.8, "rationale": "The selected tool was appropriate."},
                "tool_arguments": {"score": 0.9, "rationale": "Arguments were valid."},
                "tool_result_handling": {"score": 0.4, "rationale": "No result was handled in this round yet."},
                "evidence_grounding": {"score": 0.6, "rationale": "Evidence was partial."},
                "efficiency": {"score": 0.5, "rationale": "One extra round may be avoidable."},
                "recovery": {"score": 1.0, "rationale": "No recovery needed."},
            },
            "findings": [
                {
                    "severity": "warning",
                    "category": "round_needs_followup",
                    "dimension": "tool_result_handling",
                    "affected_surface": "loop_control",
                    "message": "The round requested a file read but did not yet consume the result.",
                    "recommendation": "Judge the follow-up round for result use.",
                    "confidence": 0.75,
                    "evidence_event_hashes": ["llm-complete"],
                }
            ],
            "confidence": 0.8,
        }
    )


def test_build_round_evidence_packs_compact_each_round_and_link_tools():
    graph = _graph()
    step = graph.steps[0]

    packs = build_round_evidence_packs(graph, step)

    assert len(packs) == 2
    pack = packs[0]
    assert pack.trace_id == "trace-1"
    assert pack.llm_call_id == "llm-1"
    assert pack.response_excerpt == "I will inspect files first."
    assert len(pack.request_messages) == 3
    assert all("old context" not in item.content_excerpt for item in pack.request_messages)
    assert pack.tool_calls[0].tool_id == "read_file"
    assert "README.md" in pack.tool_calls[0].arguments_excerpt
    assert "searchable text" in pack.tool_calls[0].result_excerpt


def test_round_evidence_links_json_action_and_unwraps_observation_value():
    graph = build_episode_graph(
        (
            _event("llm.requested", llm_call_id="trace-1-llm-1", hash="llm-request"),
            _event(
                "llm.completed",
                llm_call_id="trace-1-llm-1",
                payload={"response": {"content": '{"action":{"kind":"tool"}}', "tool_calls": []}},
                hash="llm-complete",
            ),
            _event(
                "tool.started",
                tool_call_id="trace-1-llm-1-json-tool-1",
                tool_id="read_file",
                payload={"input": {"path": "README.md"}},
                hash="tool-start",
            ),
            _event(
                "tool.completed",
                tool_call_id="trace-1-llm-1-json-tool-1",
                tool_id="read_file",
                payload={
                    "output": {
                        "at": "2026-01-01T00:00:00Z",
                        "id": "observation-1",
                        "source": "read_file",
                        "value": {"content": "useful wrapped output"},
                    }
                },
                hash="tool-complete",
            ),
        )
    )

    pack = build_round_evidence_packs(graph, graph.steps[0])[0]

    assert pack.tool_calls[0].tool_call_id == "trace-1-llm-1-json-tool-1"
    assert pack.tool_calls[0].result_excerpt == "useful wrapped output"


def test_build_round_judge_messages_include_one_round_not_full_step_history():
    graph = _graph()
    pack = build_round_evidence_packs(graph, graph.steps[0])[0]

    messages = build_round_judge_messages(pack)

    assert messages[0].role == "system"
    assert "round-level evaluator" in messages[0].content.lower()
    payload = json.loads(messages[1].content)
    assert payload["llm_call_id"] == "llm-1"
    assert "old context" not in messages[1].content
    assert len(messages[1].content) < 4000


def test_parse_round_judge_assessment_returns_structured_assessment():
    graph = _graph()
    pack = build_round_evidence_packs(graph, graph.steps[0])[0]

    parsed = parse_round_judge_assessment(_round_judge_json(), pack, evaluator_model="fake-judge-model", token_usage=TokenUsage(1, 2, 3)).unwrap()

    assert isinstance(parsed, RoundJudgeAssessment)
    assert parsed.overall == 0.64
    assert set(parsed.dimensions) == set(ROUND_JUDGE_DIMENSIONS)
    assert parsed.findings[0].category == "round_needs_followup"
    assert parsed.llm_call_id == "llm-1"


def test_build_step_judge_messages_use_round_assessments_not_full_round_trace():
    graph = _graph()
    round_pack = build_round_evidence_packs(graph, graph.steps[0])[0]
    round_assessment = parse_round_judge_assessment(
        _round_judge_json(),
        round_pack,
        evaluator_model="fake-judge-model",
        token_usage=TokenUsage(1, 2, 3),
    ).unwrap()
    pack = build_step_evidence_pack(graph, graph.steps[0], assess_steps(graph)[0], round_assessments=(round_assessment,))

    messages = build_step_judge_messages(pack)

    assert messages[0].role == "system"
    assert "step-level evaluator" in messages[0].content.lower()
    payload = json.loads(messages[1].content)
    assert payload["trace_id"] == "trace-1"
    assert payload["token_total"] == 173
    assert "round_assessments" in payload
    assert "llm_rounds" not in payload
    assert "tool_calls" not in payload
    assert "evidence_refs" not in payload
    assert "old context" not in messages[1].content
    assert "round_needs_followup" in messages[1].content


def test_parse_step_judge_assessment_returns_structured_assessment():
    graph = _graph()
    pack = build_step_evidence_pack(graph, graph.steps[0], assess_steps(graph)[0])

    parsed = parse_step_judge_assessment(_judge_json(), pack, evaluator_model="fake-judge-model", token_usage=TokenUsage(1, 2, 3)).unwrap()

    assert isinstance(parsed, StepJudgeAssessment)
    assert parsed.overall == 0.72
    assert set(parsed.dimensions) == set(JUDGE_DIMENSIONS)
    assert parsed.dimensions["tool_result_handling"].score == 0.65
    assert parsed.findings[0].category == "weak_tool_result_handling"
    assert parsed.findings[0].recommendation.startswith("Ask the model")
    assert parsed.confidence == 0.82
    assert parsed.token_usage.total_tokens == 3


def test_parse_step_judge_assessment_accepts_fenced_json_response():
    graph = _graph()
    pack = build_step_evidence_pack(graph, graph.steps[0], assess_steps(graph)[0])

    parsed = parse_step_judge_assessment(
        "```json\n" + _judge_json() + "\n```",
        pack,
        evaluator_model="fake-judge-model",
        token_usage=TokenUsage(),
    ).unwrap()

    assert parsed.overall == 0.72


def test_parse_step_judge_assessment_coerces_string_findings():
    graph = _graph()
    pack = build_step_evidence_pack(graph, graph.steps[0], assess_steps(graph)[0])
    payload = json.loads(_judge_json())
    payload["findings"] = ["The step could cite evidence more explicitly."]

    parsed = parse_step_judge_assessment(json.dumps(payload), pack, evaluator_model="fake-judge-model", token_usage=TokenUsage()).unwrap()

    assert parsed.findings[0].category == "judge_finding"
    assert parsed.findings[0].message == "The step could cite evidence more explicitly."
    assert parsed.findings[0].confidence == 0.5


def test_parse_round_judge_assessment_coerces_string_findings():
    graph = _graph()
    pack = build_round_evidence_packs(graph, graph.steps[0])[0]
    payload = json.loads(_round_judge_json())
    payload["findings"] = ["The round needs better tool result handling."]

    parsed = parse_round_judge_assessment(json.dumps(payload), pack, evaluator_model="fake-judge-model", token_usage=TokenUsage()).unwrap()

    assert parsed.findings[0].category == "judge_finding"
    assert parsed.findings[0].message == "The round needs better tool result handling."


def test_parse_step_judge_assessment_rejects_invalid_json_and_scores():
    graph = _graph()
    pack = build_step_evidence_pack(graph, graph.steps[0], assess_steps(graph)[0])

    invalid_json = parse_step_judge_assessment("not-json", pack, evaluator_model="fake-judge-model", token_usage=TokenUsage())
    assert not invalid_json.ok
    assert invalid_json.error.code == "LLM_PARSE_ERROR"

    payload = json.loads(_judge_json())
    payload["overall"] = 1.2
    invalid_score = parse_step_judge_assessment(json.dumps(payload), pack, evaluator_model="fake-judge-model", token_usage=TokenUsage())
    assert not invalid_score.ok
    assert invalid_score.error.code == "LLM_PARSE_ERROR"


def test_llm_step_judge_calls_provider_with_step_pack():
    async def scenario():
        graph = _graph()
        pack = build_step_evidence_pack(graph, graph.steps[0], assess_steps(graph)[0])
        provider = FakeJudgeProvider(_judge_json())

        result = await LlmStepJudge(provider).judge(pack)

        assert result.ok
        assert result.value.evaluator_model == "fake-judge-model"
        assert provider.messages is not None
        assert json.loads(provider.messages[1].content)["trace_id"] == "trace-1"

    asyncio.run(scenario())


def test_llm_round_judge_calls_provider_with_round_pack():
    async def scenario():
        graph = _graph()
        pack = build_round_evidence_packs(graph, graph.steps[0])[0]
        provider = FakeJudgeProvider(_round_judge_json())

        result = await LlmRoundJudge(provider).judge(pack)

        assert result.ok
        assert result.value.evaluator_model == "fake-judge-model"
        assert provider.messages is not None
        assert json.loads(provider.messages[1].content)["llm_call_id"] == "llm-1"

    asyncio.run(scenario())


def test_judge_ids_follow_disambiguated_step_and_round_identity():
    graph = build_episode_graph(
        tuple(
            event
            for loop_id in ("loop-1", "loop-2")
            for event in (
                _event("step.started", loop_id=loop_id, hash=f"{loop_id}-step"),
                _event("llm.requested", loop_id=loop_id, llm_call_id="llm-1", hash=f"{loop_id}-request"),
                _event("llm.completed", loop_id=loop_id, llm_call_id="llm-1", hash=f"{loop_id}-complete"),
            )
        )
    )
    deterministic = assess_steps(graph)
    step_results = []
    round_results = []
    for step, assessment in zip(graph.steps, deterministic, strict=True):
        step_pack = build_step_evidence_pack(graph, step, assessment)
        step_results.append(
            parse_step_judge_assessment(
                _judge_json(),
                step_pack,
                evaluator_model="fake-judge-model",
                token_usage=TokenUsage(),
            ).unwrap()
        )
        round_pack = build_round_evidence_packs(graph, step)[0]
        round_results.append(
            parse_round_judge_assessment(
                _round_judge_json(),
                round_pack,
                evaluator_model="fake-judge-model",
                token_usage=TokenUsage(),
            ).unwrap()
        )

    assert len({item.id for item in step_results}) == 2
    assert len({item.id for item in round_results}) == 2
    assert len({finding.id for item in step_results for finding in item.findings}) == 2
    assert len({finding.id for item in round_results for finding in item.findings}) == 2


def test_round_judge_finding_ids_include_round_identity():
    graph = _graph()
    packs = build_round_evidence_packs(graph, graph.steps[0])

    assessments = [
        parse_round_judge_assessment(
            _round_judge_json(),
            pack,
            evaluator_model="fake-judge-model",
            token_usage=TokenUsage(),
        ).unwrap()
        for pack in packs
    ]

    assert len({item.id for item in assessments}) == 2
    assert len({finding.id for item in assessments for finding in item.findings}) == 2
