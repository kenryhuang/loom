import asyncio
import json

from loom.core import err, make_loom_error, ok
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


class FailingScoreProvider:
    model = "fake-score-model"

    async def chat(self, messages, tools=None, cancellation=None):
        return err(
            make_loom_error(
                "LLM_FAILED",
                "Score provider failed",
                retryable=True,
                metadata={"provider": "fake"},
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

    assert isinstance(options, AnalyzeConfig)
    assert options.trace_path == tmp_path / "trace.jsonl"
    assert options.out_dir == tmp_path / "evolution"
    assert options.min_confidence == 0.8
    assert options.min_signal_frequency == 2
    assert options.max_proposals == 3


def test_package_exports_analyzer_cli_contracts():
    from loom.evolution import main as package_main
    from loom.evolution import parse_args as package_parse_args

    assert package_parse_args is not None
    assert package_main is not None


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
        assert result.value.report == result.value.artifacts.report_path.read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_analyze_trace_missing_path_returns_validation_error(tmp_path):
    async def scenario():
        result = await analyze_trace(
            AnalyzeConfig(trace_path=tmp_path / "missing.jsonl", out_dir=tmp_path / "evolution"),
            provider=FakeScoreProvider(),
        )

        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"

    asyncio.run(scenario())


def test_analyze_trace_directory_path_returns_validation_error(tmp_path):
    async def scenario():
        result = await analyze_trace(
            AnalyzeConfig(trace_path=tmp_path, out_dir=tmp_path / "evolution"),
            provider=FakeScoreProvider(),
        )

        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"

    asyncio.run(scenario())


def test_analyze_trace_malformed_jsonl_returns_error(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        trace_path.write_text("{not-json\n", encoding="utf-8")

        result = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "evolution"),
            provider=FakeScoreProvider(),
        )

        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"

    asyncio.run(scenario())


def test_analyze_trace_non_object_jsonl_record_returns_error(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        trace_path.write_text("[]\n", encoding="utf-8")

        result = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "evolution"),
            provider=FakeScoreProvider(),
        )

        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"

    asyncio.run(scenario())


def test_analyze_trace_invalid_numeric_config_returns_validation_error(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        _write_trace(trace_path)

        cases = [
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "confidence", min_confidence=1.1),
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "nan", min_confidence=float("nan")),
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "inf", min_confidence=float("inf")),
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "frequency", min_signal_frequency=0),
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "proposals", max_proposals=-1),
        ]

        for config in cases:
            result = await analyze_trace(config, provider=FakeScoreProvider())

            assert not result.ok
            assert result.error.code == "VALIDATION_FAILED"

    asyncio.run(scenario())


def test_analyze_trace_artifact_write_failure_returns_error(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evolution"
        _write_trace(trace_path)
        out_dir.write_text("not a directory", encoding="utf-8")

        result = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=out_dir, min_signal_frequency=1),
            provider=FakeScoreProvider(),
        )

        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"

    asyncio.run(scenario())


def test_analyze_trace_scorer_failure_returns_error_without_artifacts(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evolution"
        _write_trace(trace_path)

        result = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=out_dir, min_signal_frequency=1),
            provider=FailingScoreProvider(),
        )

        assert not result.ok
        assert result.error.code == "LLM_FAILED"
        assert result.error.trace_id == "trace-1"
        assert result.error.metadata["step_number"] == 0
        assert result.error.metadata["provider"] == "fake"
        assert not out_dir.exists()

    asyncio.run(scenario())
