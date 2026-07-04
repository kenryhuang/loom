import asyncio
import json
import subprocess
import sys
from pathlib import Path

from loom.core import ok
from loom.evaluation.analyze import EvaluationConfig, analyze_trace, parse_args, parse_run_options, run_evaluation_trace_with_tui
from loom.evaluation.judge import ROUND_JUDGE_DIMENSIONS
from loom.llm import LlmResponse, TokenUsage


class FakeJudgeProvider:
    model = "fake-judge-model"

    def __init__(self):
        self.messages = []

    async def chat(self, messages, tools=None, cancellation=None):
        self.messages.append(messages)
        system_prompt = (messages[0].content or "").lower()
        if "round-level evaluator" in system_prompt:
            content = {
                "overall": 0.81,
                "dimensions": {name: 0.8 for name in ROUND_JUDGE_DIMENSIONS},
                "findings": [],
                "confidence": 0.84,
            }
        else:
            content = {
                "overall": 0.71,
                "dimensions": {
                    "task_progress": 0.8,
                    "instruction_following": 0.7,
                    "tool_selection": 0.7,
                    "tool_arguments": 0.7,
                    "tool_result_handling": 0.6,
                    "evidence_grounding": 0.7,
                    "context_quality": 0.8,
                    "efficiency": 0.6,
                    "recovery": 1.0,
                },
                "findings": [
                    {
                        "severity": "warning",
                        "category": "needs_more_evidence",
                        "dimension": "evidence_grounding",
                        "affected_surface": "system_prompt",
                        "message": "The step could cite evidence more explicitly.",
                        "recommendation": "Ask for evidence hashes in audit summaries.",
                        "confidence": 0.8,
                        "evidence_event_hashes": ["trace-complete"],
                    }
                ],
                "confidence": 0.83,
            }
        return ok(
            LlmResponse(
                content=json.dumps(content),
                usage=TokenUsage(2, 3, 5),
            )
        )


class StringFindingJudgeProvider(FakeJudgeProvider):
    async def chat(self, messages, tools=None, cancellation=None):
        self.messages.append(messages)
        system_prompt = (messages[0].content or "").lower()
        if "round-level evaluator" in system_prompt:
            dimensions = {name: 0.8 for name in ROUND_JUDGE_DIMENSIONS}
        else:
            dimensions = {
                "task_progress": 0.8,
                "instruction_following": 0.7,
                "tool_selection": 0.7,
                "tool_arguments": 0.7,
                "tool_result_handling": 0.6,
                "evidence_grounding": 0.7,
                "context_quality": 0.8,
                "efficiency": 0.6,
                "recovery": 1.0,
            }
        return ok(
            LlmResponse(
                content=json.dumps({"overall": 0.71, "dimensions": dimensions, "findings": ["Judge returned a plain string finding."], "confidence": 0.83}),
                usage=TokenUsage(2, 3, 5),
            )
        )


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
            "eventType": "run.started",
            "traceId": None,
            "payload": {"type": "run.started", "run_id": "run-1", "loop_id": "loop-1"},
            "hash": "run-start",
        },
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
                "response": {"content": "Brief audit complete.", "usage": {"total_tokens": 42}},
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
        {
            "type": "event",
            "eventType": "run.completed",
            "traceId": None,
            "payload": {"type": "run.completed", "run_id": "run-1", "loop_id": "loop-1"},
            "hash": "run-complete",
        },
    ]
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")


def test_parse_args_accepts_trace_and_out_dir(tmp_path):
    config = parse_args(("--trace-path", str(tmp_path / "trace.jsonl"), "--out-dir", str(tmp_path / "eval")))

    assert config.trace_path == tmp_path / "trace.jsonl"
    assert config.out_dir == tmp_path / "eval"


def test_parse_args_accepts_optional_judge_config_and_model(tmp_path):
    config = parse_args(
        (
            "--trace-path",
            str(tmp_path / "trace.jsonl"),
            "--judge",
            "--config",
            str(tmp_path / "config.yaml"),
            "--model",
            "glm",
        )
    )

    assert config.judge is True
    assert config.config_path == tmp_path / "config.yaml"
    assert config.model_name == "glm"


def test_parse_run_options_accepts_tui_flag(tmp_path):
    options = parse_run_options(
        (
            "--trace-path",
            str(tmp_path / "trace.jsonl"),
            "--out-dir",
            str(tmp_path / "eval"),
            "--tui",
        )
    )

    assert options.config.trace_path == tmp_path / "trace.jsonl"
    assert options.config.out_dir == tmp_path / "eval"
    assert options.tui is True


def test_analyze_trace_writes_evaluation_artifacts(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)

        result = await analyze_trace(EvaluationConfig(trace_path=trace_path, out_dir=out_dir))

        assert result.ok
        assert result.value.artifacts.metrics_path.exists()
        assert result.value.artifacts.episodes_path.exists()
        assert result.value.artifacts.assessments_path.exists()
        assert result.value.artifacts.findings_path.exists()
        assert result.value.artifacts.report_path.exists()
        assert "Trace Evaluation Report" in result.value.report
        assert "## Step Assessments" in result.value.report

    asyncio.run(scenario())


def test_analyze_trace_with_judge_writes_step_judge_artifacts(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)
        provider = FakeJudgeProvider()

        result = await analyze_trace(EvaluationConfig(trace_path=trace_path, out_dir=out_dir, judge=True), judge_provider=provider)

        assert result.ok
        assert len(result.value.judge_assessments) == 1
        assert len(result.value.round_judge_assessments) == 1
        assert result.value.artifacts.judge_assessments_path.exists()
        assert result.value.artifacts.round_judge_assessments_path.exists()
        assert "## LLM Judge Assessments" in result.value.report
        assert "## Round LLM Judge Assessments" in result.value.report
        assert "needs_more_evidence" in result.value.report
        assert len(provider.messages) == 2
        assert "round-level evaluator" in provider.messages[0][0].content.lower()
        assert "step-level evaluator" in provider.messages[1][0].content.lower()

        round_rows = [json.loads(line) for line in result.value.artifacts.round_judge_assessments_path.read_text(encoding="utf-8").splitlines()]
        assert round_rows[0]["overall"] == 0.81
        judge_rows = [json.loads(line) for line in result.value.artifacts.judge_assessments_path.read_text(encoding="utf-8").splitlines()]
        assert judge_rows[0]["overall"] == 0.71

    asyncio.run(scenario())


def test_analyze_trace_with_judge_accepts_string_findings(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)

        result = await analyze_trace(EvaluationConfig(trace_path=trace_path, out_dir=out_dir, judge=True), judge_provider=StringFindingJudgeProvider())

        assert result.ok
        assert result.value.round_judge_assessments[0].findings[0].message == "Judge returned a plain string finding."
        assert result.value.judge_assessments[0].findings[0].message == "Judge returned a plain string finding."

    asyncio.run(scenario())


def test_analyze_trace_emits_tui_style_events(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)
        sink = RecordingEventSink()

        result = await analyze_trace(EvaluationConfig(trace_path=trace_path, out_dir=out_dir), event_sink=sink)

        assert result.ok
        event_types = [event["type"] for event in sink.events]
        assert event_types == [
            "run.started",
            "step.started",
            "step.completed",
            "evaluation.artifacts.written",
            "run.completed",
        ]
        assert sink.events[0]["metadata"]["role"] == "trace evaluation analyzer"
        assert sink.events[2]["trace"]["outcome"] == "evaluated"
        assert sink.events[-1]["outcome"] == "pass"
        assert sink.events[-1]["step_assessment_count"] == 1

    asyncio.run(scenario())


def test_analyze_trace_with_judge_emits_llm_events_for_tui(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)
        sink = RecordingEventSink()

        result = await analyze_trace(
            EvaluationConfig(trace_path=trace_path, out_dir=out_dir, judge=True),
            judge_provider=FakeJudgeProvider(),
            event_sink=sink,
        )

        assert result.ok
        event_types = [event["type"] for event in sink.events]
        assert "llm.requested" in event_types
        assert "llm.completed" in event_types
        llm_request = next(event for event in sink.events if event["type"] == "llm.requested")
        assert llm_request["model"] == "fake-judge-model"
        assert len(llm_request["messages"]) == 2
        assert llm_request["tools"] is None

    asyncio.run(scenario())


def test_run_evaluation_trace_with_tui_uses_shared_tui_runner(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        out_dir = tmp_path / "evaluation"
        _write_trace(trace_path)
        FakeTuiApp.instances = []

        result = await run_evaluation_trace_with_tui(
            EvaluationConfig(trace_path=trace_path, out_dir=out_dir),
            app_factory=FakeTuiApp,
        )

        assert result.ok
        app = FakeTuiApp.instances[0]
        assert app.role == "trace evaluation analyzer"
        assert app.goal == f"Evaluate trace {trace_path}"
        assert [event.event_type for event in app.events][-1] == "_tui_done"
        assert "run.started" in [event.event_type for event in app.events]

    asyncio.run(scenario())


def test_analyze_imports_trace_analysis_kernel_directly():
    source = (Path(__file__).resolve().parents[2] / "src" / "loom" / "evaluation" / "analyze.py").read_text(encoding="utf-8")

    assert "from loom.trace_analysis import EpisodeGraph, build_episode_graph, load_normalized_events" in source
    assert "from loom.evaluation.episodes import" not in source
    assert "from loom.evaluation.records import" not in source


def test_package_exports_evaluation_analyzer_tui_contracts():
    from loom.evaluation import parse_run_options as package_parse_run_options
    from loom.evaluation import run_evaluation_trace_with_tui as package_run_with_tui

    assert package_parse_run_options is not None
    assert package_run_with_tui is not None


def test_evaluation_analyze_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "loom.evaluation.analyze", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Analyze Loom trace JSONL" in result.stdout
    assert "RuntimeWarning" not in result.stderr
