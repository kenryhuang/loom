import asyncio
import json

from loom.evaluation.analyze import EvaluationConfig, analyze_trace, parse_args


def write_trace(path):
    rows = [
        {"type": "run.started", "run_id": "r", "loop_id": "l", "metadata": {"objective": "Verify greeting"}},
        {"type": "step.started", "run_id": "r", "loop_id": "l", "trace_id": "t", "step_number": 0},
        {"type": "llm.requested", "run_id": "r", "loop_id": "l", "trace_id": "t", "step_number": 0, "llm_call_id": "c",
         "messages": [{"role": "user", "content": "Say hello"}]},
        {"type": "llm.completed", "run_id": "r", "loop_id": "l", "trace_id": "t", "step_number": 0, "llm_call_id": "c",
         "response": {"content": "hello", "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}},
        {"type": "run.completed", "run_id": "r", "loop_id": "l", "outcome": "pass"},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def test_cli_defaults_to_v2_and_python_callers_retain_explicit_v1_compatibility(tmp_path):
    path = tmp_path / "trace.jsonl"
    assert parse_args(["--trace-path", str(path)]).analysis_version == "v2"
    assert parse_args(["--trace-path", str(path), "--analysis-version", "v1"]).analysis_version == "v1"
    assert EvaluationConfig(path).analysis_version == "v1"


def test_v2_analyze_writes_facts_and_unknown_semantics_without_judge(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path)
    result = asyncio.run(analyze_trace(EvaluationConfig(path, tmp_path / "out", analysis_version="v2", task="Say hello")))
    assert result.ok, result.error
    manifest = json.loads(result.value.artifacts.evaluation_bundle_path.read_text())
    assert manifest["schema_version"] == "loom.evaluation.bundle.v2"
    assert manifest["analysis"]["semantic_coverage"]["status"] == "not_evaluated"
    assert len(result.value.facts.trajectory) == 1
    assert result.value.facts.token_ledger[0]["total_tokens"] == 6
    assert not result.value.semantic.diagnoses
    assert "aggregate_score" not in result.value.report


def test_v2_malformed_trace_returns_error_and_does_not_write_success_manifest(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{bad}\n')
    result = asyncio.run(analyze_trace(EvaluationConfig(path, tmp_path / "out", analysis_version="v2")))
    assert not result.ok
    assert "line 1" in result.error.message
    assert not (tmp_path / "out/evaluation-bundle.json").exists()


def test_v2_events_work_with_real_jsonl_trace_sink(tmp_path):
    from loom.observability import JsonlTraceStore, TraceSink

    path = tmp_path / "trace.jsonl"
    write_trace(path)
    analyzer_trace = tmp_path / "analyzer.jsonl"
    result = asyncio.run(analyze_trace(EvaluationConfig(path, tmp_path / "out", analysis_version="v2"),
                                      event_sink=TraceSink(JsonlTraceStore(analyzer_trace))))
    assert result.ok, result.error
    rows = [json.loads(line) for line in analyzer_trace.read_text().splitlines()]
    assert any(row["type"] == "trace" and row["payload"]["outcome"] == "facts_recorded" for row in rows)
