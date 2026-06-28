import asyncio
import json

from loom.core import ok
from loom.evolution.analyze import AnalyzeConfig, analyze_trace, parse_args
from loom.evolution.scoring import SCORE_DIMENSIONS
from loom.llm import LlmResponse, TokenUsage


class FakeScoreProvider:
    model = "fake-score-model"

    async def chat(self, messages, tools=None, cancellation=None):
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "overall": 0.4,
                        "dimensions": {dimension: 0.3 for dimension in SCORE_DIMENSIONS},
                        "attribution": {"system_prompt": ["Output contract unclear."]},
                        "proposed_fixes": ["Clarify output contract."],
                        "confidence": 0.9,
                    }
                ),
                usage=TokenUsage(3, 4, 7),
            )
        )


def _write_trace(path):
    records = [
        {
            "type": "event",
            "eventType": "step.started",
            "traceId": "trace-1",
            "payload": {
                "type": "step.started",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
            },
            "hash": "hash-start",
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
                "messages": [],
            },
            "hash": "hash-llm-request",
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
                "response": {"content": "{}"},
            },
            "hash": "hash-llm-complete",
        },
        {
            "type": "event",
            "eventType": "step.completed",
            "traceId": "trace-1",
            "payload": {
                "type": "step.completed",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "trace_id": "trace-1",
                "step_number": 0,
            },
            "hash": "hash-step-complete",
        },
        {
            "type": "trace",
            "id": "trace-1",
            "runId": "run-1",
            "payload": {
                "id": "trace-1",
                "run_id": "run-1",
                "loop_id": "loop-1",
                "loop_version": "v1",
                "step_number": 0,
                "root_trace_id": "trace-1",
                "started_at": "2026-06-28T00:00:00Z",
                "ended_at": "2026-06-28T00:00:01Z",
                "duration_ms": 1,
                "input_context_id": "ctx-in",
                "output_context_id": "ctx-out",
                "outcome": "pass",
            },
            "hash": "hash-trace",
        },
    ]
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")


def test_parse_args_accepts_trace_and_output_paths(tmp_path):
    options = parse_args(
        (
            "--trace-path",
            str(tmp_path / "trace.jsonl"),
            "--out-dir",
            str(tmp_path / "evolution"),
            "--min-confidence",
            "0.8",
        )
    )

    assert options.trace_path == tmp_path / "trace.jsonl"
    assert options.out_dir == tmp_path / "evolution"
    assert options.min_confidence == 0.8


def test_analyze_trace_scores_and_writes_artifacts(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        _write_trace(trace_path)

        result = await analyze_trace(
            AnalyzeConfig(
                trace_path=trace_path,
                out_dir=tmp_path / "evolution",
                min_confidence=0.7,
                min_signal_frequency=1,
            ),
            provider=FakeScoreProvider(),
        )

        assert result.ok
        assert len(result.value.episodes) == 1
        assert len(result.value.scores) == 1
        assert len(result.value.signals) == 1
        assert len(result.value.proposals) == 1
        assert result.value.artifacts.report_path.exists()

    asyncio.run(scenario())
