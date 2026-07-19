from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.contracts import CandidateKind, CandidatePolicy, PromotionDisposition
from loom.campaigns.materialization import DeclarativePatchCompiler, SurfaceDefinition
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes
from loom.optimize.governance import (
    GovernanceInputs,
    OptimizeGovernanceComposer,
    bootstrap_local_governance,
    load_production_governance,
)
from loom.optimize.reporting import render_report, write_report, write_result


def _compiled(surface: str):
    if surface == "agent.loop_policy":
        base = {surface: {"max_tool_calls_per_step": 8}}
        operations = ({"op": "set_limit", "path": f"{surface}.max_tool_calls_per_step", "value": 4},)
        definition = SurfaceDefinition(surface, ("max_tool_calls_per_step",), {"max_tool_calls_per_step": int}, {"max_tool_calls_per_step": (1, 64)})
        allowed = ("set_limit", "set")
    else:
        base = {surface: {"system_prompt_addendum": "Cite evidence."}}
        operations = (
            {
                "op": "replace",
                "path": f"{surface}.system_prompt_addendum",
                "old": "Cite evidence.",
                "value": "Cite exact evidence paths.",
            },
        )
        definition = SurfaceDefinition(surface, ("system_prompt_addendum",), {"system_prompt_addendum": str})
        allowed = ("replace", "set")
    policy = CandidatePolicy((CandidateKind.DECLARATIVE_PATCH,), (surface,), (), allowed)
    return DeclarativePatchCompiler((definition,)).compile(base, operations, policy, evidence_trace_ids=("trace:seed",)).unwrap(), base


def _recommendation(artifacts, actors, candidate_id: str, source_ref):
    core = {
        "schema_version": "loom.promotion-recommendation.v1",
        "campaign_id": "cmp_test",
        "candidate_id": candidate_id,
        "candidate_artifact_ref": source_ref,
        "holdout_results": {
            candidate_id: {
                "passed": True,
                "critical_regressions": 0,
                "primary_improvement_lcb": 0.05,
            }
        },
        "disposition": "recommend",
        "activates_registry": False,
    }
    payload = {
        **core,
        "attestation": {
            "subject": actors.finalizer.subject,
            "roles": actors.finalizer.roles,
            "assertion_signature": actors.finalizer.signature,
            "payload_digest": canonical_digest(core),
        },
    }
    return artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="promotion_recommendation",
        schema_version="loom.promotion-recommendation.v1",
        suffix=".json",
    ).unwrap()


def _fixture(tmp_path: Path, surface: str):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    actors = bootstrap_local_governance(tmp_path / "inputs").unwrap()
    composer = OptimizeGovernanceComposer(artifacts, tmp_path / "governance", actors)
    candidate_id = "cand_low" if surface == "agent.loop_policy" else "cand_medium"
    source = artifacts.publish_bytes(
        canonical_json_bytes(
            {
                "schema_version": "loom.candidate-bundle.v1",
                "campaign_id": "cmp_test",
                "candidate_id": candidate_id,
                "kind": "declarative_patch",
            }
        ),
        kind="candidate_bundle",
        schema_version="loom.candidate-bundle.v1",
        suffix=".json",
    ).unwrap()
    compiled, base = _compiled(surface)
    baseline = artifacts.publish_bytes(
        canonical_json_bytes(base),
        kind="baseline_harness",
        schema_version="loom.baseline-harness.v1",
        suffix=".json",
    ).unwrap()
    evidence = artifacts.publish_bytes(
        b"paired holdout evidence",
        kind="experiment_evidence",
        schema_version="loom.experiment-evidence.v1",
    ).unwrap()
    inputs = GovernanceInputs(
        campaign_id="cmp_test",
        candidate_id=candidate_id,
        surface_id=surface,
        source_candidate_ref=source,
        recommendation_ref=_recommendation(artifacts, actors, candidate_id, source),
        compiled=compiled,
        baseline_ref=baseline,
        supporting_evidence=(evidence,),
        allowed_surfaces=("agent.loop_policy", "agent.system_prompt"),
        minimum_improvement=0.01,
    )
    return composer, inputs


@pytest.mark.asyncio
async def test_low_risk_candidate_auto_promotes_and_registers_monitor(tmp_path: Path):
    composer, inputs = _fixture(tmp_path, "agent.loop_policy")

    result = (await composer.promote(inputs)).unwrap()

    assert result.decision.decision is PromotionDisposition.PROMOTED
    monitors = (await composer.store.monitors("active")).unwrap()
    assert len(monitors) == 1
    assert result.monitor == monitors[0]


@pytest.mark.asyncio
async def test_medium_risk_candidate_waits_for_recorded_explicit_approval(tmp_path: Path):
    composer, inputs = _fixture(tmp_path, "agent.system_prompt")

    first = (await composer.promote(inputs)).unwrap()
    assert first.decision.decision is PromotionDisposition.AWAITING_APPROVAL
    assert first.monitor is None

    recorded = await composer.record_approval(inputs, first.decision, rationale="Reviewed prompt-only change")
    assert recorded.ok
    second = (await composer.promote(inputs)).unwrap()

    assert second.decision.decision is PromotionDisposition.PROMOTED
    assert second.decision.human_approval == recorded.value
    assert second.monitor is not None


@pytest.mark.asyncio
async def test_stale_approval_is_not_reused_for_changed_candidate(tmp_path: Path):
    composer, inputs = _fixture(tmp_path, "agent.system_prompt")
    pending = (await composer.promote(inputs)).unwrap()
    await composer.record_approval(inputs, pending.decision)
    changed = GovernanceInputs(
        **{
            **inputs.as_dict(),
            "candidate_id": "cand_changed",
        }
    )

    result = await composer.promote(changed)

    assert not result.ok


def test_reporting_redacts_secrets_and_hidden_holdout_content(tmp_path: Path):
    summary = {
        "optimization_id": "opt_test",
        "disposition": "awaiting_approval",
        "api_key": "sk-super-secret-value",
        "holdout": {"task_content": "hidden benchmark task", "aggregate": 0.8},
        "usage": {"total_tokens": 42},
        "next_action": "loom optimize approve opt_test",
    }

    report = render_report(summary)
    report_path = write_report(tmp_path, summary)
    result_path = write_result(tmp_path, summary)
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert "sk-super-secret-value" not in report
    assert "hidden benchmark task" not in report
    assert "sk-super-secret-value" not in result_path.read_text(encoding="utf-8")
    assert "hidden benchmark task" not in result_path.read_text(encoding="utf-8")
    assert result["api_key"] == "[REDACTED]"
    assert report_path.read_text(encoding="utf-8") == report


def test_production_identity_loading_fails_closed_for_missing_or_combined_roles(tmp_path: Path):
    missing = load_production_governance({})
    assertion = tmp_path / "finalizer.json"
    assertion.write_text(
        json.dumps(
            {
                "subject": "over-broad",
                "roles": ["campaign_finalizer", "governance_approver"],
                "issued_at": "2026-01-01T00:00:00.000000Z",
                "expires_at": "2999-01-01T00:00:00.000000Z",
                "signature": "signed",
            }
        ),
        encoding="utf-8",
    )
    env = {
        name: str(assertion)
        for name in (
            "LOOM_IDENTITY_CAMPAIGN_FINALIZER",
            "LOOM_IDENTITY_CAMPAIGN_CONTROLLER",
            "LOOM_IDENTITY_GOVERNANCE_AUTOMATION",
            "LOOM_IDENTITY_REGISTRY_OPERATOR",
            "LOOM_IDENTITY_GOVERNANCE_ADMIN",
            "LOOM_IDENTITY_GOVERNANCE_APPROVER",
        )
    }
    combined = load_production_governance(env)

    assert not missing.ok and missing.error.code == "GOVERNANCE_IDENTITY_INVALID"
    assert not combined.ok and combined.error.code == "GOVERNANCE_IDENTITY_INVALID"
