"""Mechanism-based, evidence-preserving v3 hypotheses and paired outcome gates."""

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from loom.evaluation.behavior_contracts import stable_id

SCHEMA_VERSION = "loom.evolution.hypotheses.v2"


@dataclass(frozen=True)
class BehaviorEvolutionArtifacts:
    evolution_bundle_path: Path
    report_path: Path


@dataclass(frozen=True)
class BehaviorEvolutionResult:
    proposals: tuple
    artifacts: BehaviorEvolutionArtifacts
    report: str


def proposals_from_behavior(semantic, *, source_sha256, evaluator):
    groups = defaultdict(list)
    for d in semantic.get("diagnoses", []):
        if d.get("epistemic_status") not in {"observed", "inferred"} or not d.get("supporting_refs"):
            continue
        if not d.get("improvement_hypothesis") or not d.get("intervention_surface"):
            continue
        groups[d["pattern_type"], d["mechanism_id"], d["intervention_surface"]].append(d)
    result = []
    for (pattern, mechanism, surface), rows in groups.items():
        first = rows[0]
        ready = all(first.get(k) for k in ("predicted_endpoint", "validation", "preserve", "alternatives"))
        result.append(
            {
                "schema_version": SCHEMA_VERSION,
                "id": stable_id("hypothesis", source_sha256, pattern, mechanism, surface),
                "state": "proposed",
                "readiness": "ready_for_experiment" if ready else "needs_validation_design",
                "pattern_type": pattern,
                "mechanism_id": mechanism,
                "surface": surface,
                "hypothesis": first["improvement_hypothesis"],
                "predicted_endpoint": first.get("predicted_endpoint"),
                "validation": first.get("validation"),
                "alternatives": first.get("alternatives", []),
                "preserve": list(dict.fromkeys(r.get("preserve", "") for r in rows if r.get("preserve"))),
                "finding_ids": [r["id"] for r in rows],
                "affected_scopes": [r["segment_id"] for r in rows],
                "supporting_refs": [ref for r in rows for ref in r["supporting_refs"]],
                "counterevidence_refs": [ref for r in rows for ref in r.get("counterevidence_refs", [])],
                "source_sha256": source_sha256,
                "evaluator": evaluator,
                "validation_status": "not_run",
                "quality_gate": {"goal_outcome": "non_inferiority", "preserved_behaviors": "required", "efficiency": "secondary"},
                "expected_savings": None,
            }
        )
    return result


def assess_paired_experiment(proposal, pairs):
    """Consume real matched trial records. Missing outcome or quality data is inconclusive."""
    if proposal.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Expected v2 evolution hypothesis")
    if not pairs:
        return {"state": "inconclusive", "reason": "No paired trials", "expected_savings": None}
    deltas = []
    for pair in pairs:
        baseline, candidate = pair["baseline"], pair["candidate"]
        if any(not baseline.get(k) or baseline.get(k) != candidate.get(k) for k in ("task_id", "environment_sha256", "evaluator_version")):
            return {"state": "inconclusive", "reason": "Task/environment/evaluator mismatch", "expected_savings": None}
        if baseline.get("outcome") != "achieved" or candidate.get("outcome") in {None, "unverified", "partially_verified"}:
            return {"state": "inconclusive", "reason": "Goal outcome is not verified", "expected_savings": None}
        if candidate.get("outcome") != "achieved" or candidate.get("preserved_behaviors") is False:
            return {"state": "rejected", "reason": "Outcome or preserved behavior regression", "expected_savings": None}
        if candidate.get("preserved_behaviors") is not True:
            return {"state": "inconclusive", "reason": "Preservation checks missing", "expected_savings": None}
        if any(type(row.get("endpoint_value")) not in (int, float) for row in (baseline, candidate)):
            return {"state": "inconclusive", "reason": "Predicted endpoint is unmeasured", "expected_savings": None}
        if baseline.get("endpoint") != proposal.get("predicted_endpoint") or candidate.get("endpoint") != baseline.get("endpoint"):
            return {"state": "inconclusive", "reason": "Measured endpoint differs from preregistered hypothesis", "expected_savings": None}
        deltas.append(candidate["endpoint_value"] - baseline["endpoint_value"])
    return {
        "state": "supported" if all(d < 0 for d in deltas) else "inconclusive",
        "paired_deltas": deltas,
        "reason": "Quality gates passed; lower-cost endpoint compared within matched pairs",
        "expected_savings": None,
    }


def write_behavior_evolution(config, payload):
    """Materialize v3 inputs without invoking legacy scoring or confidence gates."""
    from loom.evaluation.behavior_contracts import validate_bundle

    bundle = validate_bundle(payload)
    proposals = proposals_from_behavior(bundle["semantic"], source_sha256=bundle["source_sha256"], evaluator=bundle.get("evaluator", {}))
    proposals = proposals[: config.max_proposals]
    out = config.out_dir
    paths = [out / "evolution-bundle.json", out / "report.md"]
    if any(path.resolve() == config.evaluation_bundle_path.resolve() or (path.exists() and path.samefile(config.evaluation_bundle_path)) for path in paths):
        raise ValueError("Evolution output would overwrite its evaluation input")
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_evaluation_bundle": str(config.evaluation_bundle_path),
        "source_sha256": bundle["source_sha256"],
        "proposals": proposals,
        "summary": {"proposal_count": len(proposals), "validation_status": "not_run"},
    }
    report = "# Behavior evolution hypotheses\n\nAll proposals are untested. No legacy scores were generated.\n\n"
    for row in proposals:
        report += f"## {row['surface']} · {row['state']}\n\n{row['hypothesis']}\n\nValidation: {row['validation'] or 'Needs design'}\n\n"
    paths[0].write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths[1].write_text(report, encoding="utf-8")

    return BehaviorEvolutionResult(tuple(proposals), BehaviorEvolutionArtifacts(*paths), report)
