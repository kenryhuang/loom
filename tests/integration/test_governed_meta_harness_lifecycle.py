from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loom.campaigns.config import load_campaign_spec
from loom.campaigns.contracts import ExperimentPhase
from loom.campaigns.controller import CampaignController, publish_phase_results
from loom.campaigns.operations import CampaignOperation
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, new_prefixed_id
from loom.campaigns.store import SQLiteCampaignStore
from loom.governance.gates import GateEvidence
from loom.governance.monitor import MonitoringService
from loom.governance.policy import ActorAssertion, StaticIdentityProvider
from loom.governance.promotion import (
    GovernedPromotionController,
    PromotionRequest,
    publish_candidate_evidence,
    publish_gate_evidence,
    publish_gate_source_evidence,
)
from loom.governance.registry import MonitorRegistration, SQLiteGovernanceStore
from tests.campaigns.conftest import make_candidate_evidence


def artifact(kind: str, char: str):
    from loom.campaigns.contracts import ArtifactRef

    return ArtifactRef(f"{kind}.v1", kind, f"{kind}-{char}.json", char * 64, 1)


def campaign_actor(role: str) -> ActorAssertion:
    return ActorAssertion(
        role,
        (role,),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        f"signed-{role}",
    )


def make_campaign_spec(store, tmp_path: Path):
    task_paths = {}
    for role in ("discovery", "validation", "holdout"):
        path = tmp_path / f"{role}.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(
                    {
                        "task_id": f"{role}-{index}",
                        "owner": "loom",
                        "source_evidence_ref": f"evidence:{role}:{index}",
                        "use_basis": "internal-test",
                        "project_snapshot_digest": canonical_digest({"role": role, "index": index}),
                        "template_lineage": f"template-{role}-{index}",
                        "sanitizer_version": "sanitizer.v1",
                        "content": f"{role} unique task {index}",
                    }
                )
                for index in range(2)
            )
            + "\n",
            encoding="utf-8",
        )
        task_paths[f"{role}_set"] = str(path)
    config = {
        "schema_version": "loom.campaign.spec.v1",
        "objective": "Improve audit quality.",
        "baseline": {"kind": "task_profile", "ref": "audit@v1"},
        "solver": {"model_profile": "main", "temperature": 0},
        "proposer": {"adapter": "manual", "model": "none"},
        "evaluator": {"deterministic_profile": "agent_task_v1"},
        "environment": {"image_digest": "a" * 64, "runner_version": "runner.v1", "isolation_profile": "declarative"},
        "tools_digest": "b" * 64,
        "permissions_digest": "c" * 64,
        "editable_surfaces": ["context_numeric_limit"],
        "forbidden_surfaces": ["evaluator", "permissions"],
        **task_paths,
        "objectives": [
            {
                "id": "task_success",
                "direction": "maximize",
                "hard": True,
                "absolute_limit": 0.5,
                "max_baseline_regression": 0.0,
                "min_valid_pairs": 5,
                "missing_policy": "fail_closed",
            }
        ],
        "budget": {
            "iterations": 2,
            "candidates_per_iteration": 2,
            "phases": [
                {"phase": "discovery", "max_candidate_experiments": 2, "max_task_side_runs": 12},
                {"phase": "validation", "max_candidate_experiments": 1, "max_task_side_runs": 12},
                {"phase": "holdout", "max_candidate_experiments": 1, "max_task_side_runs": 12},
            ],
            "infrastructure_retry_task_side_runs": 2,
            "proposer_tokens": 1000,
            "solver_tokens": 2000,
            "maximum_cost": "10",
            "wall_time_seconds": 300,
        },
        "monitoring": {"ttl_runs": 20, "minimum_sample_size": 5, "quality_regression_threshold": 0.05},
    }
    config_path = tmp_path / "campaign-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return load_campaign_spec(config_path, store.artifacts).unwrap()


def test_governed_meta_harness_replays_campaign_promotes_and_rolls_back(tmp_path: Path):
    async def scenario():
        automation = ActorAssertion(
            "automation",
            ("governance_automation",),
            "2026-07-18T00:00:00.000000Z",
            "2999-01-01T00:00:00.000000Z",
            "signed",
        )
        monitor_actor = ActorAssertion(
            "monitor",
            ("monitor_service",),
            "2026-07-18T00:00:00.000000Z",
            "2999-01-01T00:00:00.000000Z",
            "monitor-signed",
        )
        governance_admin = ActorAssertion(
            "governance-admin",
            ("governance_admin",),
            "2026-07-18T00:00:00.000000Z",
            "2999-01-01T00:00:00.000000Z",
            "governance-admin-signed",
        )
        campaign_actors = tuple(campaign_actor(role) for role in ("campaign_creator", "campaign_operator", "campaign_controller", "campaign_finalizer")) + (
            automation,
        )
        campaign_store = SQLiteCampaignStore(tmp_path / "campaign", StaticIdentityProvider(campaign_actors))
        spec = make_campaign_spec(campaign_store, tmp_path)
        await campaign_store.create(spec, operation_id=new_prefixed_id("op_"), actor=campaign_actor("campaign_creator"))
        started = await campaign_store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("start"), "campaign.started", campaign_actor("campaign_operator")),
            1,
        )
        controller = CampaignController(
            campaign_store,
            spec,
            controller_actor=campaign_actor("campaign_controller"),
            finalizer_actor=campaign_actor("campaign_finalizer"),
        )
        invalid_candidate, invalid_validation, _ = make_candidate_evidence(campaign_store, spec, "cand_invalid", valid=False)
        await controller.record_candidate(
            "cand_invalid",
            candidate_ref=invalid_candidate,
            validation_ref=invalid_validation,
            experiment_ref=None,
        )
        candidate_ref, candidate_validation, discovery_experiment = make_candidate_evidence(campaign_store, spec, "cand_good", primary=0.80)
        await controller.record_candidate(
            "cand_good",
            candidate_ref=candidate_ref,
            validation_ref=candidate_validation,
            experiment_ref=discovery_experiment,
        )
        projection = (await campaign_store.load(spec.campaign_id)).unwrap()
        paused = await campaign_store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("pause"), "campaign.paused", campaign_actor("campaign_operator")),
            projection.aggregate_version,
        )
        resumed = await campaign_store.transact(
            CampaignOperation(new_prefixed_id("op_"), spec.campaign_id, canonical_digest("resume"), "campaign.resumed", campaign_actor("campaign_operator")),
            paused.unwrap().aggregate_version,
        )
        assert resumed.ok and started.ok

        sealed = await controller.seal_search(new_prefixed_id("op_"), controller.frontier_digest())
        validation = publish_phase_results(
            campaign_store,
            spec,
            "validation",
            sealed.value.entrant_digest,
            (make_candidate_evidence(campaign_store, spec, "cand_good", primary=0.75, phase=ExperimentPhase.VALIDATION)[2],),
        ).unwrap()
        finalists = await controller.select_finalists(validation)
        holdout = publish_phase_results(
            campaign_store,
            spec,
            "holdout",
            finalists.value.finalist_digest,
            (make_candidate_evidence(campaign_store, spec, "cand_good", primary=0.75, phase=ExperimentPhase.HOLDOUT)[2],),
        ).unwrap()
        recommendation = await controller.finalize(
            finalists.unwrap().finalist_digest,
            holdout,
            operation_id=new_prefixed_id("op_"),
        )
        assert sealed.ok and recommendation.ok
        assert recommendation.value.active_artifact is None

        exported = (await campaign_store.export(spec.campaign_id)).unwrap()
        exported_payload = json.loads(campaign_store.artifacts.read_bytes(exported).unwrap())
        rebuilt = (await campaign_store.rebuild(spec.campaign_id)).unwrap()
        assert exported_payload["projection"] == rebuilt.to_dict()
        assert rebuilt.candidates["cand_invalid"].lifecycle.value == "invalid"
        assert rebuilt.candidates["cand_good"].lifecycle.value == "holdout_passed"

        artifacts = campaign_store.artifacts
        finalizer = campaign_actor("campaign_finalizer")
        campaign_controller = campaign_actor("campaign_controller")
        governance_identities = StaticIdentityProvider((automation, monitor_actor, finalizer, governance_admin, campaign_controller))
        governance_store = SQLiteGovernanceStore(tmp_path / "governance", governance_identities, artifacts)
        baseline = artifacts.publish_bytes(b"baseline", kind="baseline", schema_version="baseline.v1").unwrap()
        policy = artifacts.publish_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "loom.governance-policy.v1",
                    "promotion": {
                        "allowed_surfaces": ["context_numeric_limit"],
                        "auto_promote_max_risk": "low",
                        "executable_requires_human": True,
                        "require_holdout": True,
                        "minimum_improvement": 0.01,
                    },
                    "monitoring": {
                        "minimum_sample_size": 3,
                        "quality_regression_threshold": 0.05,
                        "ttl_runs": 20,
                        "heartbeat_grace_seconds": 60,
                    },
                }
            ),
            kind="policy",
            schema_version="loom.governance-policy.v1",
        ).unwrap()
        risk_rules = artifacts.publish_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "loom.risk-rule-set.v1",
                    "implementation_version": "loom.default-risk-rules.v1",
                }
            ),
            kind="risk_rules",
            schema_version="loom.risk-rule-set.v1",
        ).unwrap()
        evidence_ref = artifacts.publish_bytes(b"evidence", kind="evidence", schema_version="evidence.v1").unwrap()
        active_candidate = artifacts.publish_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "loom.governed-candidate.v1",
                    "campaign_id": spec.campaign_id,
                    "candidate_id": "cand_good",
                    "source_candidate_ref": candidate_ref,
                    "kind": "declarative_patch",
                    "operations": [{"op": "set_limit", "path": "context_numeric_limit.value", "value": 128}],
                    "capability_manifest": {
                        "imports": [],
                        "dependencies": [],
                        "filesystem_read_roots": [],
                        "filesystem_write_roots": [],
                        "callable_tools": [],
                        "input_schema": {},
                        "output_schema": {},
                        "resource_limits": {},
                    },
                    "materialized": {"context_numeric_limit": {"value": 128}},
                }
            ),
            kind="governed_candidate",
            schema_version="loom.governed-candidate.v1",
        ).unwrap()
        candidate = publish_candidate_evidence(
            artifacts,
            spec.campaign_id,
            "cand_good",
            active_candidate,
            recommendation.value.report_ref,
            finalizer,
            governance_identities,
        ).unwrap()
        await governance_store.authorize_governance_artifact(policy, "policy", governance_admin)
        await governance_store.authorize_governance_artifact(risk_rules, "risk_rules", governance_admin)
        await governance_store.initialize_surface("context_numeric_limit", baseline, governance_admin)
        gate_source = publish_gate_source_evidence(
            artifacts,
            spec.campaign_id,
            "cand_good",
            {
                "task_set_isolation": True,
                "contamination_free": True,
                "evaluator_independent": True,
                "sandbox_conformant": True,
                "security_regression_passed": True,
            },
            (evidence_ref,),
            campaign_controller,
            governance_identities,
        ).unwrap()
        evidence = GateEvidence(
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            0,
            0.05,
            0.01,
            True,
            (gate_source,),
        )
        gate_evidence = publish_gate_evidence(
            artifacts,
            evidence,
            spec.campaign_id,
            "cand_good",
            candidate,
            finalizer,
            governance_identities,
        ).unwrap()
        promotion = GovernedPromotionController(
            governance_store,
            artifacts,
            risk_rules,
            campaign_store=campaign_store,
            campaign_actor=automation,
        )
        promoted = await promotion.promote(
            PromotionRequest(
                spec.campaign_id,
                "cand_good",
                "context_numeric_limit",
                candidate,
                baseline,
                baseline.sha256,
                policy,
                gate_evidence,
                None,
                new_prefixed_id("op_"),
                "2026-07-18T00:00:00.000000Z",
            ),
            automation,
            MonitorRegistration(
                "monitor",
                "context_numeric_limit",
                "pending",
                True,
                0,
                3,
                0.05,
                ttl_runs=20,
                heartbeat_grace_seconds=60,
                policy_digest=policy.sha256,
            ),
        )
        assert promoted.ok
        assert (await governance_store.active("context_numeric_limit")).unwrap().artifact == active_candidate
        campaign_projection = (await campaign_store.load(spec.campaign_id)).unwrap()
        assert campaign_projection.candidates["cand_good"].lifecycle.value == "promoted"
        assert campaign_projection.candidates["cand_good"].latest_governance_ref is not None

        monitoring = MonitoringService(governance_store, monitor_actor)
        await monitoring.record("monitor", 1, quality_delta=-0.10)
        await monitoring.record("monitor", 1, quality_delta=-0.08)
        requested = await monitoring.record("monitor", 1, quality_delta=-0.09)
        rolled_back = await governance_store.execute_monitor_rollback("monitor", 1, automation)
        active = (await governance_store.active("context_numeric_limit")).unwrap()
        assert requested.unwrap().action == "rollback_requested"
        assert rolled_back.unwrap().action == "rolled_back"
        assert active.artifact == baseline
        assert active.version == 2

    asyncio.run(scenario())
