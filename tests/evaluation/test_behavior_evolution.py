from loom.evaluation.calibration import calibration_report
from loom.evolution.behavior import SCHEMA_VERSION, assess_paired_experiment, proposals_from_behavior


def diagnosis(text):
    return {
        "id": text,
        "segment_id": text,
        "dimension": "progress_effectiveness",
        "pattern_type": "repeated_unchanged_result",
        "mechanism_id": "output_truncation",
        "intervention_surface": "tool.read_artifact",
        "epistemic_status": "inferred",
        "supporting_refs": [{"line_number": 1}],
        "counterevidence_refs": [{"line_number": 2}],
        "improvement_hypothesis": text,
        "predicted_endpoint": "tokens",
        "validation": "Compare full and paged retrieval under the same citation oracle",
        "preserve": "All required citations",
        "alternatives": ["Context eviction"],
    }


def test_group_mechanisms_not_exact_prose_and_do_not_map_dimension_to_surface():
    a, b = diagnosis("Use pagination"), diagnosis("Read subsequent pages")
    c = {**diagnosis("Retain results"), "mechanism_id": "context_eviction", "intervention_surface": "context.policy"}
    proposals = proposals_from_behavior({"diagnoses": [a, b, c]}, source_sha256="s", evaluator={"model": "judge"})
    assert len(proposals) == 2
    assert proposals[0]["finding_ids"] == ["Use pagination", "Read subsequent pages"]
    assert proposals[0]["surface"] == "tool.read_artifact"
    assert proposals[0]["state"] == "proposed"
    assert proposals[0]["readiness"] == "ready_for_experiment"
    assert proposals[0]["expected_savings"] is None
    assert proposals[0]["counterevidence_refs"]


def test_cheaper_candidate_that_omits_verification_is_not_supported():
    proposal = {"schema_version": SCHEMA_VERSION, "predicted_endpoint": "tokens"}
    baseline = {
        "task_id": "t",
        "environment_sha256": "env",
        "evaluator_version": "v3",
        "outcome": "achieved",
        "endpoint": "tokens",
        "endpoint_value": 100,
        "preserved_behaviors": True,
    }
    candidate = {**baseline, "endpoint_value": 20, "preserved_behaviors": False}
    result = assess_paired_experiment(proposal, [{"baseline": baseline, "candidate": candidate}])
    assert result["state"] == "rejected"
    candidate.update(preserved_behaviors=True, outcome="unverified")
    assert assess_paired_experiment(proposal, [{"baseline": baseline, "candidate": candidate}])["state"] == "inconclusive"
    candidate["outcome"] = "achieved"
    assert assess_paired_experiment(proposal, [{"baseline": baseline, "candidate": candidate}])["state"] == "supported"


def test_missing_calibration_never_enables_rollout():
    report = calibration_report([])
    assert report["rollout_ready"] is False
    assert report["reviewed_held_out_episodes"] == 0
    assert report["cost"]["median_v3_v2_ratio"] is None


def test_evolution_cli_consumes_v3_without_legacy_score_adapter(tmp_path):
    import asyncio
    import json

    from loom.evolution.analyze import AnalyzeConfig, analyze_trace

    source = tmp_path / "evaluation.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": "loom.evaluation.bundle.v3",
                "source_sha256": "source",
                "base": {},
                "facts": {},
                "semantic": {"diagnoses": [diagnosis("Read the remaining pages")]},
            }
        )
    )
    result = asyncio.run(analyze_trace(AnalyzeConfig(evaluation_bundle_path=source, out_dir=tmp_path / "evolve"))).unwrap()
    bundle = json.loads(result.artifacts.evolution_bundle_path.read_text())
    assert bundle["schema_version"] == SCHEMA_VERSION
    assert bundle["proposals"][0]["surface"] == "tool.read_artifact"
    assert not hasattr(result, "scores")
