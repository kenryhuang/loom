import asyncio
import json
import subprocess
import sys

from loom.evaluation.analyze import EvaluationConfig, analyze_trace, parse_args


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
