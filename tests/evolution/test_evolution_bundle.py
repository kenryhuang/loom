import asyncio
import json

from loom.evolution.analyze import AnalyzeConfig, analyze_trace, parse_args
from loom.evolution.bundle import load_evaluation_bundle, signals_from_evaluation_bundle


class ExplodingProvider:
    async def chat(self, messages, tools=None, cancellation=None):
        raise AssertionError("provider should not be called for evaluation-bundle analysis")


def _write_evaluation_bundle(base):
    base.mkdir()
    for name in ("episodes.jsonl", "metrics.jsonl", "step-assessments.jsonl", "round-judge-assessments.jsonl", "judge-assessments.jsonl"):
        (base / name).write_text("", encoding="utf-8")
    finding = {
        "id": "eval-finding-1",
        "source": "round_judge",
        "severity": "warning",
        "category": "tool_call_missing",
        "dimension": "tool_selection",
        "surface": "tool_call_parser",
        "message": "The response described an action but no tool call was emitted.",
        "recommendation": "Validate action text against tool call structure.",
        "confidence": 0.8,
        "impact_score": 0.4,
        "frequency_key": "tool_call_parser:tool_call_missing:tool_selection",
        "step_ref": {"run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0},
        "round_ref": {"run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0, "round_index": 0, "llm_call_id": "llm-1"},
        "evidence_refs": [
            {
                "event_hash": "hash-llm-complete",
                "event_type": "llm.completed",
                "subject_id": "llm:run-1:trace-1:0:llm-1",
                "field_path": "response.content",
                "excerpt": "I should inspect a file.",
            }
        ],
    }
    (base / "findings.jsonl").write_text(json.dumps(finding, sort_keys=True) + "\n", encoding="utf-8")
    (base / "evidence-index.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "hash-llm-complete",
                "event_hash": "hash-llm-complete",
                "event_type": "llm.completed",
                "subject_id": "llm:run-1:trace-1:0:llm-1",
                "field_path": "response.content",
                "excerpt": "I should inspect a file.",
                "source_trace_path": "trace.jsonl",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (base / "report.md").write_text("# Trace Evaluation Report\n", encoding="utf-8")
    manifest = {
        "schema_version": "loom.evaluation.bundle.v1",
        "bundle_id": "eval-test",
        "created_at": "2026-07-05T00:00:00Z",
        "source_trace": {"path": "trace.jsonl", "kind": "jsonl", "sha256": None},
        "summary": {
            "runs": 1,
            "steps": 1,
            "llm_rounds": 1,
            "tool_calls": 0,
            "metrics": 0,
            "step_assessments": 0,
            "round_judge_assessments": 0,
            "step_judge_assessments": 0,
            "findings": 1,
        },
        "artifacts": {
            "episodes": "episodes.jsonl",
            "metrics": "metrics.jsonl",
            "step_assessments": "step-assessments.jsonl",
            "round_judges": "round-judge-assessments.jsonl",
            "step_judges": "judge-assessments.jsonl",
            "findings": "findings.jsonl",
            "evidence_index": "evidence-index.jsonl",
            "report": "report.md",
        },
    }
    path = base / "evaluation-bundle.json"
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def test_signals_from_evaluation_bundle_groups_normalized_findings(tmp_path):
    bundle = load_evaluation_bundle(_write_evaluation_bundle(tmp_path / "evaluation")).unwrap()

    signals = signals_from_evaluation_bundle(bundle, min_frequency=1)

    assert len(signals) == 1
    assert signals[0].kind == "evaluation_finding"
    assert signals[0].surface == "tool_call_parser"
    assert signals[0].frequency == 1
    assert signals[0].trace_ids == ("trace-1",)
    assert signals[0].evidence_event_hashes == ("hash-llm-complete",)


def test_parse_args_accepts_evaluation_bundle_without_trace_path(tmp_path):
    bundle_path = tmp_path / "evaluation" / "evaluation-bundle.json"

    config = parse_args(("--evaluation-bundle", str(bundle_path), "--out-dir", str(tmp_path / "evolution")))

    assert config.trace_path is None
    assert config.evaluation_bundle_path == bundle_path


def test_analyze_trace_with_evaluation_bundle_skips_provider_and_writes_evolution_bundle(tmp_path):
    async def scenario():
        bundle_path = _write_evaluation_bundle(tmp_path / "evaluation")

        result = await analyze_trace(
            AnalyzeConfig(evaluation_bundle_path=bundle_path, out_dir=tmp_path / "evolution", min_signal_frequency=1),
            provider=ExplodingProvider(),
        )

        assert result.ok
        assert result.value.episodes == ()
        assert result.value.scores == ()
        assert len(result.value.signals) == 1
        assert len(result.value.proposals) == 1
        assert result.value.artifacts.evolution_bundle_path.exists()
        manifest = json.loads(result.value.artifacts.evolution_bundle_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "loom.evolution.bundle.v1"
        assert manifest["source_evaluation_bundle"] == str(bundle_path)

    asyncio.run(scenario())
