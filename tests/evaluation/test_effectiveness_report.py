import json

import pytest


def test_report_exposes_unverified_requirements_and_measured_cost_without_quality_score(tmp_path):
    from loom.evaluation.analysis_report import write_effectiveness_artifacts
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import SemanticAnalysis, unverified_criteria
    from loom.evaluation.evidence_store import EvidenceStore

    p = tmp_path / "trace.jsonl"
    p.write_text(json.dumps({"type": "run.completed", "run_id": "r", "outcome": "pass"}) + '\n')
    store = EvidenceStore.open(p)
    facts = FactAnalysis(task_contracts=({"id": "task:r", "objective": "Test it", "criteria": [{"id": "c", "description": "Behavior correct"}]},),
                         token_ledger=({"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14, "usage_status": "recorded"},),
                         coverage={"usage_complete": True})
    semantic = SemanticAnalysis((), unverified_criteria(facts), (), (), {"status": "not_evaluated"}, {})
    result = write_effectiveness_artifacts(tmp_path / "out", store, facts, semantic, config={"judge": False})
    manifest = json.loads(result.evaluation_bundle_path.read_text())
    assert manifest["schema_version"] == "loom.evaluation.bundle.v2"
    assert manifest["source_trace"]["sha256"] == store.source_sha256
    assert manifest["analysis"]["semantic_coverage"]["status"] == "not_evaluated"
    for path in manifest["artifacts"].values():
        assert (result.out_dir / path).exists()
    report = result.report_path.read_text()
    assert "unverified" in report
    assert "14" in report
    assert "aggregate_score" not in report
    assert "context_effectiveness" in report


def test_v1_bundle_consumer_reports_version_mismatch_before_parsing_v2_fields(tmp_path):
    from loom.evaluation.bundle import load_evaluation_bundle

    path = tmp_path / "evaluation-bundle.json"
    path.write_text(json.dumps({"schema_version": "loom.evaluation.bundle.v2", "analysis": {}}))
    result = load_evaluation_bundle(path)
    assert not result.ok
    assert "Unsupported evaluation bundle schema version" in result.error.message


def test_artifact_writer_never_overwrites_registered_source(tmp_path):
    from loom.evaluation.analysis_report import write_effectiveness_artifacts
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import SemanticAnalysis
    from loom.evaluation.evidence_store import EvidenceStore

    p = tmp_path / "trajectory.jsonl"
    original = '{"type":"run.started","run_id":"r"}\n'
    p.write_text(original)
    store = EvidenceStore.open(p)
    semantic = SemanticAnalysis((), (), (), (), {"status": "not_evaluated"}, {})
    with pytest.raises(ValueError, match="source"):
        write_effectiveness_artifacts(tmp_path, store, FactAnalysis(), semantic, config={})
    assert p.read_text() == original
