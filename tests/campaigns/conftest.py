from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from loom.campaigns.contracts import (
    CampaignBudget,
    CampaignSpec,
    CandidateKind,
    CandidatePolicy,
    EnvironmentSpec,
    EvaluatorSpec,
    ExperimentPhase,
    ExperimentStatus,
    HistoryVisibilityPolicy,
    MonitoringPolicy,
    ObjectiveDirection,
    ObjectiveSpec,
    PhaseBudget,
    PromotionPolicy,
    ProposerSpec,
    SolverSpec,
    TaskSetRef,
)
from loom.campaigns.experiments import freeze_trial_plan, publish_experiment_bundle
from loom.campaigns.serialization import canonical_digest, new_prefixed_id, utc_now
from loom.campaigns.store import SQLiteCampaignStore
from loom.campaigns.task_sets import TaskManifestRow, fingerprint_task_rows
from loom.campaigns.validation import ValidationResult, publish_validation_evidence
from loom.core import ActorAssertion, StaticIdentityProvider
from loom.evaluation.experiments import ExperimentBundle, ExperimentUsage, PairedMetricResult, RunArtifactRef, TrialPlan

_ACTORS = {
    role: ActorAssertion(
        role,
        (role,),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        f"signed-{role}",
    )
    for role in ("campaign_creator", "campaign_operator", "campaign_controller", "campaign_finalizer")
}


def campaign_actor(role: str) -> ActorAssertion:
    return _ACTORS[role]


def make_campaign_store(path: Path) -> SQLiteCampaignStore:
    return SQLiteCampaignStore(path, StaticIdentityProvider(tuple(_ACTORS.values())))


def make_campaign_spec(store, *, discovery_experiments: int = 2, discovery_runs: int = 20) -> CampaignSpec:
    baseline = store.artifacts.publish_bytes(b'{"profile":"baseline"}', kind="baseline", schema_version="baseline.v1", suffix=".json").unwrap()
    task_refs = []
    for role in (ExperimentPhase.DISCOVERY, ExperimentPhase.VALIDATION, ExperimentPhase.HOLDOUT):
        rows = tuple(
            TaskManifestRow(
                f"{role.value}-{index}",
                "loom",
                f"evidence:{role.value}:{index}",
                "internal-test",
                canonical_digest({"role": role.value, "index": index}),
                f"template:{role.value}:{index}",
                "sanitizer.v1",
                f"unique {role.value} task content {index}",
            )
            for index in range(2)
        )
        fingerprinted = fingerprint_task_rows(rows, role).unwrap()
        content = "\n".join(json.dumps(asdict(row)) for row in rows)
        manifest = store.artifacts.publish_bytes(
            (content + "\n").encode(),
            kind="task_set",
            schema_version="loom.task-set.manifest.v1",
            suffix=".jsonl",
        ).unwrap()
        task_refs.append(TaskSetRef(f"tasks-{role.value}", role, manifest, fingerprinted.fingerprint_digest))
    return CampaignSpec(
        schema_version="loom.campaign.spec.v1",
        campaign_id=new_prefixed_id("cmp_"),
        created_at=utc_now(),
        derived_from=None,
        objective="Improve project audit quality without increasing cost.",
        baseline=baseline,
        solver=SolverSpec("main", canonical_digest({"model": "solver"}), {"temperature": 0}),
        proposer=ProposerSpec("manual", "none"),
        evaluator=EvaluatorSpec("agent_task_v1", None, canonical_digest({"evaluator": "v1"})),
        environment=EnvironmentSpec(canonical_digest({"image": "local"}), "runner.v1", "declarative"),
        tools_digest=canonical_digest({"tools": ["read_file"]}),
        permissions_digest=canonical_digest({"permissions": ["workspace:read"]}),
        editable_surfaces=("tool_description", "context_policy", "completion_policy"),
        forbidden_surfaces=("evaluator", "permissions", "provider", "secrets"),
        discovery_set=task_refs[0],
        validation_set=task_refs[1],
        holdout_set=task_refs[2],
        objectives=(
            ObjectiveSpec(
                "task_success",
                ObjectiveDirection.MAXIMIZE,
                hard=True,
                absolute_limit=0.5,
                max_baseline_regression=0.0,
                min_valid_pairs=5,
                missing_policy="fail_closed",
            ),
        ),
        budget=CampaignBudget(
            iterations=2,
            candidates_per_iteration=2,
            phases=(
                PhaseBudget(ExperimentPhase.DISCOVERY, discovery_experiments, discovery_runs),
                PhaseBudget(ExperimentPhase.VALIDATION, 1, 12),
                PhaseBudget(ExperimentPhase.HOLDOUT, 1, 12),
            ),
            infrastructure_retry_task_side_runs=2,
            proposer_tokens=1000,
            solver_tokens=2000,
            maximum_cost=Decimal("10"),
            wall_time_seconds=300,
        ),
        candidate_policy=CandidatePolicy(
            kinds=(CandidateKind.DECLARATIVE_PATCH,),
            editable_surfaces=("tool_description", "context_policy", "completion_policy"),
            forbidden_surfaces=("evaluator", "permissions", "provider", "secrets"),
            allowed_patch_operations=("replace", "set"),
        ),
        history_visibility=HistoryVisibilityPolicy(
            ("surface", "message", "details", "findings", "metrics"),
            ("event_type", "excerpt"),
        ),
        promotion_policy=PromotionPolicy(),
        monitoring_policy=MonitoringPolicy(20, 5, 0.05),
    )


def make_candidate_evidence(
    store,
    spec: CampaignSpec,
    candidate_id: str,
    *,
    valid: bool = True,
    primary: float = 0.8,
    phase: ExperimentPhase = ExperimentPhase.DISCOVERY,
):
    candidate_ref = store.artifacts.publish_bytes(
        (f'{{"schema_version":"loom.candidate-bundle.v1","campaign_id":"{spec.campaign_id}","candidate_id":"{candidate_id}"}}').encode(),
        kind="candidate_bundle",
        schema_version="loom.candidate-bundle.v1",
        suffix=".json",
    ).unwrap()
    validation_ref = publish_validation_evidence(
        store,
        campaign_id=spec.campaign_id,
        candidate_id=candidate_id,
        candidate_ref=candidate_ref,
        result=ValidationResult(valid, ("manifest", "declarative_patch", "inverse_smoke") if valid else ("manifest",)),
        policy_digest=canonical_digest(spec.candidate_policy),
        actor=campaign_actor("campaign_controller"),
    ).unwrap()
    if not valid:
        return candidate_ref, validation_ref, None
    task_set = {
        ExperimentPhase.DISCOVERY: spec.discovery_set,
        ExperimentPhase.VALIDATION: spec.validation_set,
        ExperimentPhase.HOLDOUT: spec.holdout_set,
    }[phase]
    evaluation_ref = store.artifacts.publish_bytes(
        f'{{"schema_version":"loom.evaluation.bundle.v1","bundle_id":"eval-{candidate_id}-{phase.value}"}}'.encode(),
        kind="evaluation_bundle",
        schema_version="loom.evaluation.bundle.v1",
    ).unwrap()
    trace_ref = store.artifacts.publish_bytes(b"", kind="trace", schema_version="loom.trace.v1").unwrap()
    entries = freeze_trial_plan(store, task_set).unwrap().entries
    baseline_runs = tuple(RunArtifactRef(f"trial-{index}", f"baseline-{index}", trace_ref, f"b{index}", "completed") for index in range(len(entries)))
    candidate_runs = tuple(RunArtifactRef(f"trial-{index}", f"candidate-{index}", trace_ref, f"c{index}", "completed") for index in range(len(entries)))
    metric = PairedMetricResult(
        "task_success",
        0.6,
        primary,
        primary - 0.6,
        len(entries),
        (0.6, 0.6),
        (primary, primary),
        (primary - 0.6, primary - 0.6),
        0.0,
    )
    bundle = ExperimentBundle(
        "loom.experiment.bundle.v1",
        new_prefixed_id("exp_"),
        spec.campaign_id,
        candidate_id,
        utc_now(),
        spec.baseline,
        phase,
        task_set,
        spec.environment.image_digest,
        spec.solver.model_digest,
        spec.tools_digest,
        spec.permissions_digest,
        spec.evaluator.digest,
        TrialPlan(entries),
        (evaluation_ref,) * len(entries),
        (evaluation_ref,) * len(entries),
        baseline_runs,
        candidate_runs,
        (metric,),
        (),
        ExperimentStatus.COMPLETED,
        ExperimentUsage(len(entries) * 2, 0, 100, "0.10", 10),
    )
    experiment_ref = publish_experiment_bundle(store, bundle, actor=campaign_actor("campaign_controller")).unwrap()
    return candidate_ref, validation_ref, experiment_ref
