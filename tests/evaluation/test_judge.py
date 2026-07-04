import asyncio
import json

from loom.core import ok
from loom.evaluation.assessments import assess_steps
from loom.evaluation.episodes import build_episode_graph
from loom.evaluation.judge import (
    JUDGE_DIMENSIONS,
    LlmStepJudge,
    StepJudgeAssessment,
    build_step_evidence_pack,
    build_step_judge_messages,
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
            _event("llm.requested", llm_call_id="llm-1", payload={"messages": [{"role": "user", "content": "Audit the project briefly."}]}, hash="llm-request"),
            _event(
                "llm.completed",
                llm_call_id="llm-1",
                payload={
                    "response": {
                        "content": "I will inspect files first.",
                        "finish_reason": "tool_calls",
                        "tool_calls": [{"id": "call-1", "name": "read_file", "arguments": "{\"path\":\"README.md\"}"}],
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


def test_build_step_evidence_pack_compacts_step_for_judge():
    graph = _graph()
    step = graph.steps[0]
    assessment = assess_steps(graph)[0]

    pack = build_step_evidence_pack(graph, step, assessment)

    assert pack.trace_id == "trace-1"
    assert pack.token_total == 173
    assert pack.deterministic_status == "pass"
    assert pack.llm_rounds[0].llm_call_id == "llm-1"
    assert pack.llm_rounds[0].response_excerpt == "I will inspect files first."
    assert pack.tool_calls[0].tool_id == "read_file"
    assert "README.md" in pack.tool_calls[0].arguments_excerpt
    assert "searchable text" in pack.tool_calls[0].result_excerpt
    assert "tool-complete" in {ref.event_hash for ref in pack.evidence_refs}


def test_build_step_judge_messages_include_compact_pack_not_raw_trace_dump():
    graph = _graph()
    pack = build_step_evidence_pack(graph, graph.steps[0], assess_steps(graph)[0])

    messages = build_step_judge_messages(pack)

    assert messages[0].role == "system"
    assert "step-level evaluator" in messages[0].content.lower()
    payload = json.loads(messages[1].content)
    assert payload["trace_id"] == "trace-1"
    assert payload["token_total"] == 173
    assert "events" not in payload
    assert "This README excerpt is intentionally long" not in messages[1].content
    assert "YakDB turns files into searchable text" in payload["tool_calls"][0]["result_excerpt"]


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
