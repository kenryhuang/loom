import asyncio
import json
import subprocess
import sys

from loom.core import err, make_loom_error, ok
from loom.evolution.analyze import AnalyzeConfig, analyze_trace, parse_args, parse_run_options, run_analyze_trace_with_tui
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


class RecordingScoreProvider(FakeScoreProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None):
        self.calls += 1
        return await super().chat(messages, tools=tools, cancellation=cancellation)


class RecordingEventSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event):
        self.events.append(event)
        return ok(None)


class FakeTuiApp:
    instances = []

    def __init__(self, collector) -> None:
        self.collector = collector
        self.role = ""
        self.goal = ""
        self.events = []
        FakeTuiApp.instances.append(self)

    def set_loop_info(self, *, role, goal):
        self.role = role
        self.goal = goal

    async def run_async(self):
        while True:
            event = await self.collector.queue.get()
            self.events.append(event)
            if event.event_type == "_tui_done":
                return


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


def _write_incomplete_trace(path):
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


def test_parse_run_options_accepts_tui_flag(tmp_path):
    options = parse_run_options(
        (
            "--trace-path",
            str(tmp_path / "trace.jsonl"),
            "--out-dir",
            str(tmp_path / "evolution"),
            "--tui",
        )
    )

    assert options.config.trace_path == tmp_path / "trace.jsonl"
    assert options.config.out_dir == tmp_path / "evolution"
    assert options.tui is True


def test_package_exports_analyzer_cli_contracts():
    from loom.evolution import main as package_main
    from loom.evolution import parse_args as package_parse_args
    from loom.evolution import parse_run_options as package_parse_run_options

    assert package_parse_args is not None
    assert package_parse_run_options is not None
    assert package_main is not None


def test_analyze_module_cli_help_does_not_warn_about_preimported_module():
    result = subprocess.run(
        [sys.executable, "-m", "loom.evolution.analyze", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "RuntimeWarning" not in result.stderr
    assert "Analyze Loom trace JSONL" in result.stdout


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


def test_analyze_trace_emits_tui_style_events(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        _write_trace(trace_path)
        sink = RecordingEventSink()

        result = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "evolution", min_signal_frequency=1),
            provider=FakeScoreProvider(),
            event_sink=sink,
        )

        assert result.ok
        event_types = [event["type"] for event in sink.events]
        assert event_types == [
            "run.started",
            "step.started",
            "llm.requested",
            "llm.completed",
            "step.completed",
            "evolution.signals.generated",
            "evolution.proposals.generated",
            "evolution.artifacts.written",
            "run.completed",
        ]
        request = sink.events[2]
        assert request["model"] == "fake-score-model"
        assert len(request["messages"]) == 2
        assert request["tools"] is None
        assert sink.events[4]["trace"]["outcome"] == "scored"
        assert sink.events[-1]["outcome"] == "pass"
        assert sink.events[-1]["proposal_count"] == 1

    asyncio.run(scenario())


def test_run_analyze_trace_with_tui_uses_shared_tui_runner(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        _write_trace(trace_path)
        FakeTuiApp.instances = []

        result = await run_analyze_trace_with_tui(
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "evolution", min_signal_frequency=1),
            provider=FakeScoreProvider(),
            app_factory=FakeTuiApp,
        )

        assert result.ok
        app = FakeTuiApp.instances[0]
        assert app.role == "trace evolution analyzer"
        assert app.goal == f"Analyze trace {trace_path}"
        assert [event.event_type for event in app.events][-1] == "_tui_done"
        assert "llm.requested" in [event.event_type for event in app.events]

    asyncio.run(scenario())


def test_analyze_trace_rejects_incomplete_episodes_without_scoring_or_artifacts(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evolution"
        _write_incomplete_trace(trace_path)
        provider = RecordingScoreProvider()

        result = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=out_dir, min_signal_frequency=1),
            provider=provider,
        )

        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"
        assert result.error.metadata["incomplete_trace_ids"] == ("trace-1",)
        assert provider.calls == 0
        assert not out_dir.exists()

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
