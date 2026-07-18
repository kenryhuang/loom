import asyncio
import json

from loom.core import ok
from loom.evaluation.analyze import EvaluationConfig, analyze_trace
from loom.evaluation.bundle import EvaluationFinding, load_evaluation_bundle
from loom.evaluation.judge import ROUND_JUDGE_DIMENSIONS
from loom.llm import LlmResponse, TokenUsage


class FakeJudgeProvider:
    model = "fake-judge-model"

    async def chat(self, messages, tools=None, cancellation=None):
        system = (messages[0].content or "").lower()
        if "round-level evaluator" in system:
            dimensions = {name: 0.8 for name in ROUND_JUDGE_DIMENSIONS}
            findings = [
                {
                    "severity": "warning",
                    "category": "tool_call_missing",
                    "dimension": "tool_selection",
                    "affected_surface": "unknown",
                    "message": "The response described an action but no tool call was emitted.",
                    "recommendation": "Validate action text against tool call structure.",
                    "confidence": 0.75,
                    "evidence_event_hashes": ["llm-complete"],
                },
                {
                    "severity": "warning",
                    "category": "tool_call_missing",
                    "dimension": "round_progress",
                    "affected_surface": "unknown",
                    "message": "The round still needs inherited evidence when the judge omits finding-level hashes.",
                    "recommendation": "Fall back to the round evidence pack hashes.",
                    "confidence": 0.75,
                },
            ]
        else:
            dimensions = {
                "task_progress": 0.8,
                "instruction_following": 0.8,
                "tool_selection": 0.8,
                "tool_arguments": 0.8,
                "tool_result_handling": 0.8,
                "evidence_grounding": 0.7,
                "context_quality": 0.8,
                "efficiency": 0.8,
                "recovery": 1.0,
            }
            findings = []
        return ok(
            LlmResponse(
                content=json.dumps({"overall": 0.75, "dimensions": dimensions, "findings": findings, "confidence": 0.8}),
                usage=TokenUsage(2, 3, 5),
            )
        )


def _write_trace(path):
    records = [
        {
            "type": "event",
            "eventType": "step.started",
            "traceId": "trace-1",
            "payload": {"type": "step.started", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0},
            "hash": "step-start",
        },
        {
            "type": "event",
            "eventType": "llm.requested",
            "traceId": "trace-1",
            "payload": {
                "type": "llm.requested",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
                "llm_call_id": "llm-1",
                "messages": [{"role": "user", "content": "Audit this project briefly."}],
            },
            "hash": "llm-request",
        },
        {
            "type": "event",
            "eventType": "llm.completed",
            "traceId": "trace-1",
            "payload": {
                "type": "llm.completed",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
                "llm_call_id": "llm-1",
                "response": {"content": "I should inspect a file.", "usage": {"total_tokens": 42}},
            },
            "hash": "llm-complete",
        },
        {
            "type": "event",
            "eventType": "step.completed",
            "traceId": "trace-1",
            "payload": {"type": "step.completed", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0},
            "hash": "step-complete",
        },
        {
            "type": "trace",
            "id": "trace-1",
            "runId": "run-1",
            "payload": {"id": "trace-1", "run_id": "run-1", "loop_id": "loop-1", "step_number": 0, "outcome": "pass"},
            "hash": "trace-complete",
        },
    ]
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")


def test_evaluation_writes_loadable_bundle_and_normalized_findings(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)

        result = await analyze_trace(EvaluationConfig(trace_path=trace_path, out_dir=out_dir, judge=True), judge_provider=FakeJudgeProvider())

        assert result.ok
        assert result.value.artifacts.evaluation_bundle_path.exists()
        assert result.value.artifacts.evidence_index_path.exists()
        loaded = load_evaluation_bundle(result.value.artifacts.evaluation_bundle_path).unwrap()
        assert loaded.manifest.schema_version == "loom.evaluation.bundle.v1"
        assert loaded.manifest.source_trace.path == str(trace_path)
        assert loaded.manifest.artifacts.findings == "findings.jsonl"
        assert all(path.exists() for path in loaded.artifact_paths().values())
        assert isinstance(loaded.findings[0], EvaluationFinding)
        assert loaded.findings[0].source == "round_judge"
        assert loaded.findings[0].surface == "tool_call_parser"
        assert loaded.findings[0].frequency_key == "tool_call_parser:tool_call_missing:tool_selection"
        assert loaded.findings[0].impact_score > 0
        assert loaded.findings[0].evidence_refs[0].event_hash == "llm-complete"
        assert {ref.event_hash for ref in loaded.findings[1].evidence_refs} == {"llm-request", "llm-complete"}

    asyncio.run(scenario())
