"""Default composition for the one-command governed Meta-Harness workflow."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.contracts import (
    ApprovalRecord,
    ArtifactRef,
    CampaignBudget,
    CampaignSpec,
    CandidateBundle,
    CandidateKind,
    CandidatePolicy,
    EnvironmentSpec,
    EvaluatorSpec,
    ExperimentPhase,
    ExperimentStatus,
    GateDecision,
    GateResult,
    HistoryVisibilityPolicy,
    MonitoringPolicy,
    ObjectiveDirection,
    ObjectiveSpec,
    PhaseBudget,
    PromotionDecision,
    PromotionDisposition,
    PromotionPolicy,
    ProposerSpec,
    RiskLevel,
    SolverSpec,
)
from loom.campaigns.controller import CampaignController, publish_phase_results
from loom.campaigns.experiments import (
    BaselineRunCache,
    PairedExperimentRunner,
    ValidatedCandidate,
    freeze_trial_plan,
    load_experiment_bundle,
    publish_experiment_bundle,
)
from loom.campaigns.history import CampaignHistory, HistoryRecord, import_experience
from loom.campaigns.materialization import CompiledDeclarativeCandidate, DeclarativePatchCompiler, SurfaceDefinition
from loom.campaigns.operations import CampaignOperation
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, prefixed_id_from_digest, utc_now
from loom.campaigns.store import SQLiteCampaignStore
from loom.campaigns.validation import DefaultCandidateValidator, publish_validation_evidence
from loom.campaigns.workspace import CandidateWorkspace
from loom.core import ActorAssertion, Result, StaticIdentityProvider, err, make_loom_error, ok, thaw_json
from loom.evaluation import EvaluationConfig, load_evaluation_bundle
from loom.evaluation import analyze_trace as analyze_evaluation_trace
from loom.evolution import AnalyzeConfig
from loom.evolution import analyze_trace as analyze_evolution_trace
from loom.optimize.config import load_optimize_config
from loom.optimize.contracts import LoadedOptimizeConfig, OptimizationResult, OptimizationSpec
from loom.optimize.governance import (
    GovernanceInputs,
    OptimizeGovernanceComposer,
    bootstrap_local_governance,
    load_production_governance,
)
from loom.optimize.orchestrator import CampaignOutcome, OptimizeCampaignServices, OptimizeOrchestrator
from loom.optimize.proposer import LoomNativeProposerAdapter
from loom.optimize.reporting import write_report, write_result
from loom.optimize.store import SQLiteOptimizationStore
from loom.optimize.task_sets import (
    PreparedTaskSet,
    PreparedTaskSets,
    PublishedTaskSets,
    prepare_explicit_task_sets,
    prepare_task_sets,
    publish_prepared_task_sets,
)
from loom.optimize.trial_executor import OptimizeTrialExecutor
from loom.optimize.ui import create_observer
from loom.tasks import TaskHarness, create_provider_from_task_config

_ALL_TASK_TOOLS = ("read_file", "edit_file", "write_file", "shell_execute", "finish")
_FORBIDDEN_SURFACES = ("evaluator", "permissions", "provider", "secrets", "governance", "runtime_engine")


@dataclass(frozen=True, slots=True)
class _CampaignActors:
    creator: ActorAssertion
    operator: ActorAssertion
    controller: ActorAssertion
    finalizer: ActorAssertion


@dataclass(frozen=True, slots=True)
class _CandidateRuntime:
    candidate_id: str
    surface_id: str
    source_ref: ArtifactRef
    compiled: CompiledDeclarativeCandidate
    harness: TaskHarness


class DefaultOptimizeRuntime:
    def __init__(self, orchestrator: OptimizeOrchestrator, services: DefaultOptimizeCampaignServices, prepared: PreparedTaskSets):
        self.orchestrator = orchestrator
        self.services = services
        self.prepared = prepared
        self.spec = orchestrator.spec

    async def run(self) -> Result:
        return await self.orchestrator.run()

    def dry_run(self) -> Result:
        return ok(
            {
                "schema_version": "loom.optimization.dry-run.v1",
                "disposition": "dry_run",
                "optimization_id": self.spec.optimization_id,
                "optimization_key": self.spec.optimization_key,
                "campaign_id": self.spec.campaign_id,
                "task_set_digests": dict(self.spec.task_set_digests),
                "task_counts": {
                    "discovery": len(self.prepared.discovery.tasks),
                    "validation": len(self.prepared.validation.tasks),
                    "holdout": len(self.prepared.holdout.tasks),
                },
                "model_digests": dict(self.spec.model_digests),
                "estimated_max_candidates": self.services.loaded.meta.budgets.max_candidates,
            }
        )


class DefaultOptimizeCampaignServices:
    def __init__(
        self,
        *,
        loaded: LoadedOptimizeConfig,
        root: Path,
        trace_path: Path,
        prepared: PreparedTaskSets,
        published: PublishedTaskSets,
        campaign_store: SQLiteCampaignStore,
        campaign_spec: CampaignSpec,
        campaign_actors: _CampaignActors,
        governance_actors,
        proposer_provider: Any,
        solver_provider: Any,
        judge_provider: Any,
    ):
        self.loaded = loaded
        self.root = root
        self.trace_path = trace_path
        self.prepared = prepared
        self.published = published
        self.store = campaign_store
        self.spec = campaign_spec
        self.actors = campaign_actors
        self.governance_actors = governance_actors
        self.proposer_provider = proposer_provider
        self.solver_provider = solver_provider
        self.judge_provider = judge_provider
        self.baseline_harness = _baseline_harness(loaded)
        self.compiler = DeclarativePatchCompiler(_surface_definitions(self.baseline_harness))
        self.controller = CampaignController(
            campaign_store,
            campaign_spec,
            controller_actor=campaign_actors.controller,
            finalizer_actor=campaign_actors.finalizer,
        )
        self.history = CampaignHistory(campaign_store, campaign_spec, campaign_actors.controller)
        self.proposer: LoomNativeProposerAdapter | None = None
        self.candidates: dict[str, _CandidateRuntime] = {}
        self.validation_results_ref: ArtifactRef | None = None
        self.finalist_digest: str | None = None
        self.holdout_results_ref: ArtifactRef | None = None
        self.holdout_experiment_refs: tuple[ArtifactRef, ...] = ()
        self._baseline_cache = BaselineRunCache()

    def contract(self) -> OptimizeCampaignServices:
        return OptimizeCampaignServices(
            preflight=self.preflight,
            seed_evaluation=self.seed_evaluation,
            seed_evolution=self.seed_evolution,
            initialize_campaign=self.initialize_campaign,
            search_iteration=self.search_iteration,
            seal_search=self.seal_search,
            validate=self.validate,
            select_finalists=self.select_finalists,
            holdout=self.holdout,
            finalize=self.finalize,
        )

    def preflight(self) -> Result:
        return ok(
            {
                "trace_digest": _file_digest(self.trace_path),
                "task_set_digests": self.prepared.fingerprint_digests,
                "task_counts": {
                    "discovery": len(self.prepared.discovery.tasks),
                    "validation": len(self.prepared.validation.tasks),
                    "holdout": len(self.prepared.holdout.tasks),
                },
                "campaign_spec_digest": canonical_digest(self.spec),
            }
        )

    async def seed_evaluation(self) -> Result:
        path = self.root / "seed" / "evaluation" / "evaluation-bundle.json"
        cached = load_evaluation_bundle(path) if path.is_file() else None
        if cached is not None and cached.ok:
            return self._publish_seed_bundle(path, kind="evaluation_bundle", schema_version="loom.evaluation.bundle.v1")
        analyzed = await analyze_evaluation_trace(
            EvaluationConfig(
                self.trace_path,
                out_dir=self.root / "seed" / "evaluation",
                judge=True,
            ),
            judge_provider=self.judge_provider,
        )
        if not analyzed.ok:
            return analyzed
        path = analyzed.value.artifacts.evaluation_bundle_path
        return self._publish_seed_bundle(path, kind="evaluation_bundle", schema_version="loom.evaluation.bundle.v1")

    async def seed_evolution(self, evaluation: Mapping[str, Any]) -> Result:
        bundle_path = Path(str(evaluation["bundle_path"]))
        path = self.root / "seed" / "evolution" / "evolution-bundle.json"
        if _valid_evolution_bundle(path, bundle_path):
            return self._publish_seed_bundle(path, kind="evolution_bundle", schema_version="loom.evolution.bundle.v1")
        analyzed = await analyze_evolution_trace(
            AnalyzeConfig(
                evaluation_bundle_path=bundle_path,
                out_dir=self.root / "seed" / "evolution",
            )
        )
        if not analyzed.ok:
            return analyzed
        path = analyzed.value.artifacts.evolution_bundle_path
        return self._publish_seed_bundle(path, kind="evolution_bundle", schema_version="loom.evolution.bundle.v1")

    def _publish_seed_bundle(self, path: Path, *, kind: str, schema_version: str) -> Result:
        published = self.store.artifacts.publish_bytes(
            path.read_bytes(),
            kind=kind,
            schema_version=schema_version,
            suffix=".json",
        )
        if not published.ok:
            return published
        output_key = "evaluation_ref" if kind == "evaluation_bundle" else "evolution_ref"
        return ok({output_key: asdict(published.value), "bundle_path": str(path)})

    async def initialize_campaign(self, seed: Mapping[str, Any]) -> Result:
        created = await self.store.create(
            self.spec,
            operation_id=_operation_id("campaign.created", self.spec.campaign_id),
            actor=self.actors.creator,
        )
        if not created.ok:
            return created
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        if projection.value.lifecycle.value == "created":
            started = await self.store.transact(
                CampaignOperation(
                    _operation_id("campaign.started", self.spec.campaign_id),
                    self.spec.campaign_id,
                    canonical_digest({"event": "campaign.started", "campaign_id": self.spec.campaign_id}),
                    "campaign.started",
                    self.actors.operator,
                ),
                projection.value.aggregate_version,
            )
            if not started.ok:
                return started
        for name in ("evaluation", "evolution"):
            imported = await import_experience(
                self.store,
                self.spec.campaign_id,
                str(seed[name]["bundle_path"]),
                operation_id=_operation_id("experience.imported", name),
                actor=self.actors.creator,
            )
            if not imported.ok:
                return imported
        evidence_refs = (
            _artifact_ref(seed["evaluation"]["evaluation_ref"]).sha256,
            _artifact_ref(seed["evolution"]["evolution_ref"]).sha256,
        )
        self.history = CampaignHistory(
            self.store,
            self.spec,
            self.actors.controller,
            records=(
                HistoryRecord(
                    "finding",
                    "seed-analysis",
                    {
                        "surface": "agent.loop_policy",
                        "message": "Seed evaluation and evolution evidence is available.",
                        "details": {"evidence_refs": evidence_refs},
                    },
                ),
            ),
        )
        self.proposer = LoomNativeProposerAdapter(
            self.proposer_provider,
            self.baseline_harness,
            evidence_refs=evidence_refs,
        )
        restored = await self.controller.restore()
        if not restored.ok:
            return restored
        return ok({"campaign_id": self.spec.campaign_id, "evidence_refs": evidence_refs})

    async def search_iteration(self, iteration: int) -> Result:
        if self.proposer is None:
            seed = await self._optimization_stage_output("seed_analysis")
            if not seed.ok:
                return seed
            initialized = await self.initialize_campaign(seed.value)
            if not initialized.ok:
                return initialized
        workspace_root = self.root / "candidate-workspaces"
        workspace_path = workspace_root / f"iteration-{iteration}"
        if workspace_path.exists():
            workspace = CandidateWorkspace(workspace_path.resolve())
        else:
            allocated = CandidateWorkspace.allocate(workspace_root, f"iteration-{iteration}")
            if not allocated.ok:
                return allocated
            workspace = allocated.value

        async def evaluate(draft, candidate_id):
            return await self._evaluate_draft(draft, candidate_id, workspace, ExperimentPhase.DISCOVERY)

        result = await self.controller.run_iteration(iteration, self.proposer, self.history, workspace, evaluate)
        if not result.ok:
            return result
        return ok({"candidate_ids": result.value})

    async def seal_search(self, candidate_ids: tuple[str, ...]) -> Result:
        del candidate_ids
        sealed = await self.controller.seal_search(
            _operation_id("campaign.search_sealed", self.spec.campaign_id),
            self.controller.frontier_digest(),
        )
        if not sealed.ok:
            return sealed
        return ok(
            {
                "entrant_ids": sealed.value.validation_entrants,
                "entrant_digest": sealed.value.entrant_digest,
                "frontier_digest": sealed.value.frontier_digest,
            }
        )

    async def validate(self, entrant_ids: tuple[str, ...]) -> Result:
        refs = []
        for candidate_id in entrant_ids:
            experiment = await self._run_experiment(candidate_id, ExperimentPhase.VALIDATION)
            if not experiment.ok:
                return experiment
            refs.append(experiment.value)
        seal = await self._optimization_stage_output("search_sealed")
        if not seal.ok:
            return seal
        phase = publish_phase_results(
            self.store,
            self.spec,
            "validation",
            str(seal.value["entrant_digest"]),
            tuple(refs),
        )
        if not phase.ok:
            return phase
        summary = self._experiment_summary(tuple(refs))
        if not summary.ok:
            return summary
        self.validation_results_ref = phase.value
        return ok(
            {
                "validated_ids": entrant_ids,
                "experiment_refs": tuple(asdict(ref) for ref in refs),
                "results_ref": asdict(phase.value),
                **summary.value,
            }
        )

    async def select_finalists(self, validated_ids: tuple[str, ...]) -> Result:
        del validated_ids
        validation_ref = self.validation_results_ref
        if validation_ref is None:
            output = await self._optimization_stage_output("validation")
            if not output.ok:
                return output
            validation_ref = _artifact_ref(output.value["results_ref"])
        selected = await self.controller.select_finalists(validation_ref)
        if not selected.ok:
            return selected
        self.finalist_digest = selected.value.finalist_digest
        return ok(
            {
                "finalist_ids": selected.value.candidate_ids,
                "finalist_digest": selected.value.finalist_digest,
            }
        )

    async def holdout(self, finalist_ids: tuple[str, ...]) -> Result:
        refs = []
        for candidate_id in finalist_ids:
            experiment = await self._run_experiment(candidate_id, ExperimentPhase.HOLDOUT)
            if not experiment.ok:
                return experiment
            refs.append(experiment.value)
        finalist_digest = self.finalist_digest
        if finalist_digest is None:
            output = await self._optimization_stage_output("finalists")
            if not output.ok:
                return output
            finalist_digest = str(output.value["finalist_digest"])
        phase = publish_phase_results(self.store, self.spec, "holdout", finalist_digest, tuple(refs))
        if not phase.ok:
            return phase
        summary = self._experiment_summary(tuple(refs))
        if not summary.ok:
            return summary
        self.holdout_results_ref = phase.value
        self.holdout_experiment_refs = tuple(refs)
        return ok(
            {
                "evaluated_ids": finalist_ids,
                "experiment_refs": tuple(asdict(ref) for ref in refs),
                "results_ref": asdict(phase.value),
                **summary.value,
            }
        )

    async def finalize(self, finalist_ids: tuple[str, ...], holdout: Mapping[str, Any]) -> Result:
        if not finalist_ids:
            return ok({"disposition": "reject", "candidate_id": None, "recommendation_ref": None})
        finalist_digest = self.finalist_digest
        if finalist_digest is None:
            output = await self._optimization_stage_output("finalists")
            if not output.ok:
                return output
            finalist_digest = str(output.value["finalist_digest"])
        results_ref = self.holdout_results_ref or _artifact_ref(holdout["results_ref"])
        finalized = await self.controller.finalize(
            finalist_digest,
            results_ref,
            operation_id=_operation_id("campaign.finalized", finalist_digest),
        )
        if not finalized.ok:
            return finalized
        disposition = "recommend" if finalized.value.disposition == "recommend" else "reject"
        return ok(
            {
                "disposition": disposition,
                "candidate_id": finalized.value.candidate_id,
                "recommendation_ref": asdict(finalized.value.report_ref),
            }
        )

    async def govern(self, outcome: CampaignOutcome) -> Result:
        if outcome.candidate_id is None or outcome.recommendation_ref is None:
            return ok(
                {
                    "disposition": "rejected",
                    "candidate_id": None,
                    "promotion_decision_ref": None,
                    "monitor_ref": None,
                }
            )
        runtime = await self._candidate(outcome.candidate_id)
        if not runtime.ok:
            return runtime
        holdout = await self._optimization_stage_output("holdout")
        if not holdout.ok:
            return holdout
        holdout_value = holdout.value.get("holdout", holdout.value)
        supporting = tuple(_artifact_ref(value) for value in holdout_value.get("experiment_refs", ()))
        results = holdout_value.get("results_ref")
        if results is not None:
            supporting = (*supporting, _artifact_ref(results))
        if not supporting:
            return _runtime_error("GOVERNANCE_EVIDENCE_MISSING", "Holdout evidence is unavailable")
        inputs = GovernanceInputs(
            self.spec.campaign_id,
            outcome.candidate_id,
            runtime.value.surface_id,
            runtime.value.source_ref,
            _artifact_ref(outcome.recommendation_ref),
            runtime.value.compiled,
            self.spec.baseline,
            supporting,
            self.spec.editable_surfaces,
            self.spec.promotion_policy.minimum_improvement,
            minimum_sample_size=max(1, self.loaded.meta.tasks.minimum_pairs),
            regression_threshold=self.loaded.meta.objectives.max_regression_rate,
        )
        composer = OptimizeGovernanceComposer(self.store.artifacts, self.root / "governance", self.governance_actors)
        governed = await composer.promote(inputs)
        if not governed.ok:
            return governed
        if governed.value.decision.decision.value == "awaiting_approval":
            _write_json(
                self.root / "approval-request.json",
                {
                    "schema_version": "loom.optimization.approval-request.v1",
                    "candidate_id": outcome.candidate_id,
                    "decision": asdict(governed.value.decision),
                    "decision_ref": asdict(governed.value.decision_ref),
                    "governance_mode": self.loaded.meta.governance.mode,
                    "governance_inputs": _governance_input_payload(inputs),
                },
            )
        return ok(governed.value)

    async def _evaluate_draft(self, draft, candidate_id: str, workspace: CandidateWorkspace, phase: ExperimentPhase) -> Result:
        artifact_path = workspace.resolve(draft.artifact_path)
        if not artifact_path.ok:
            return artifact_path
        patch_path = workspace.resolve(draft.patch_path or "")
        if not patch_path.ok:
            return patch_path
        artifact_ref = self.store.artifacts.publish_bytes(
            artifact_path.value.read_bytes(),
            kind="candidate_artifact",
            schema_version="loom.native-candidate-artifact.v1",
            suffix=".json",
        )
        if not artifact_ref.ok:
            return artifact_ref
        patch_ref = self.store.artifacts.publish_bytes(
            patch_path.value.read_bytes(),
            kind="candidate_patch",
            schema_version="loom.declarative-patch.v1",
            suffix=".json",
        )
        if not patch_ref.ok:
            return patch_ref
        session_ref = self.store.artifacts.publish_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "loom.proposer-session.v1",
                    "campaign_id": self.spec.campaign_id,
                    "candidate_id": candidate_id,
                    "proposer": self.spec.proposer.model,
                }
            ),
            kind="proposer_session",
            schema_version="loom.proposer-session.v1",
            suffix=".json",
        )
        if not session_ref.ok:
            return session_ref
        bundle = CandidateBundle(
            "loom.candidate-bundle.v1",
            candidate_id,
            self.spec.campaign_id,
            self.spec.created_at,
            draft.kind,
            draft.parent_ids,
            draft.inspiration_ids,
            session_ref.value,
            draft.hypothesis,
            draft.evidence_refs,
            draft.changed_surfaces,
            artifact_ref.value,
            patch_ref.value,
            draft.capability_manifest,
        )
        candidate_ref = self.store.artifacts.publish_bytes(
            canonical_json_bytes(bundle),
            kind="candidate_bundle",
            schema_version="loom.candidate-bundle.v1",
            suffix=".json",
        )
        if not candidate_ref.ok:
            return candidate_ref
        validated = await DefaultCandidateValidator(workspace, self.compiler).validate(draft, self.spec.candidate_policy)
        if not validated.ok:
            return validated
        validation_ref = publish_validation_evidence(
            self.store,
            campaign_id=self.spec.campaign_id,
            candidate_id=candidate_id,
            candidate_ref=candidate_ref.value,
            result=validated.value,
            policy_digest=canonical_digest(self.spec.candidate_policy),
            actor=self.actors.controller,
        )
        if not validation_ref.ok:
            return validation_ref
        if not validated.value.valid or validated.value.compiled is None:
            return ok((candidate_ref.value, validation_ref.value, None))
        surface = draft.changed_surfaces[0]
        runtime = _CandidateRuntime(
            candidate_id,
            surface,
            candidate_ref.value,
            validated.value.compiled,
            _task_harness(validated.value.compiled.materialized),
        )
        self.candidates[candidate_id] = runtime
        experiment = await self._run_experiment(candidate_id, phase)
        if not experiment.ok:
            return experiment
        return ok((candidate_ref.value, validation_ref.value, experiment.value))

    async def _run_experiment(self, candidate_id: str, phase: ExperimentPhase) -> Result:
        runtime = await self._candidate(candidate_id)
        if not runtime.ok:
            return runtime
        task_set = {
            ExperimentPhase.DISCOVERY: self.published.discovery,
            ExperimentPhase.VALIDATION: self.published.validation,
            ExperimentPhase.HOLDOUT: self.published.holdout,
        }[phase]
        checkpoint = self.root / "evidence" / "experiments" / phase.value / f"{candidate_id}.json"
        restored = self._restore_experiment(checkpoint, runtime.value, phase, task_set)
        if not restored.ok:
            return restored
        if restored.value is not None:
            return ok(restored.value)
        plan = freeze_trial_plan(self.store, task_set, repetitions=self.loaded.meta.tasks.repetitions)
        if not plan.ok:
            return plan
        executor = OptimizeTrialExecutor(
            self.prepared,
            self.store.artifacts,
            workspace_root=self.root / "trials" / phase.value,
            solver_provider=self.solver_provider,
            candidate_harnesses={candidate_id: runtime.value.harness},
            judge_provider=self.judge_provider,
            trial_timeout_seconds=self.loaded.meta.execution.trial_timeout_seconds,
            verifier_timeout_seconds=self.loaded.meta.execution.verifier_timeout_seconds,
        )
        candidate = ValidatedCandidate(
            self.spec.campaign_id,
            candidate_id,
            runtime.value.source_ref,
            self.spec.baseline,
            self.spec.environment.image_digest,
            self.spec.solver.model_digest,
            self.spec.tools_digest,
            self.spec.permissions_digest,
            self.spec.evaluator.digest,
        )
        experiment_id = prefixed_id_from_digest(
            "exp_",
            canonical_digest(
                {
                    "campaign_id": self.spec.campaign_id,
                    "candidate_id": candidate_id,
                    "phase": phase.value,
                    "task_set": task_set.fingerprint_digest,
                }
            ),
        )
        runner = PairedExperimentRunner(
            executor.execute,
            objectives=self.spec.objectives,
            infrastructure_retries=1,
            baseline_cache=self._baseline_cache,
        )
        evaluated = await runner.evaluate(candidate, task_set, plan.value, experiment_id=experiment_id)
        if not evaluated.ok:
            return evaluated
        published = publish_experiment_bundle(self.store, evaluated.value, actor=self.actors.controller)
        if not published.ok:
            return published
        _write_json(
            checkpoint,
            {
                "schema_version": "loom.optimization.experiment-checkpoint.v1",
                "campaign_id": self.spec.campaign_id,
                "candidate_id": candidate_id,
                "candidate_ref": asdict(runtime.value.source_ref),
                "phase": phase.value,
                "task_set_digest": task_set.fingerprint_digest,
                "experiment_ref": asdict(published.value),
            },
        )
        return published

    def _restore_experiment(self, checkpoint: Path, runtime: _CandidateRuntime, phase: ExperimentPhase, task_set) -> Result:
        if not checkpoint.is_file():
            return ok(None)
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            experiment_ref = _artifact_ref(payload["experiment_ref"])
            candidate_ref = _artifact_ref(payload["candidate_ref"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return _runtime_error("EXPERIMENT_CHECKPOINT_INVALID", "Experiment checkpoint is malformed", cause=exc)
        loaded = load_experiment_bundle(self.store, experiment_ref)
        if not loaded.ok:
            return loaded
        bundle = loaded.value
        expected = (
            payload.get("schema_version") == "loom.optimization.experiment-checkpoint.v1"
            and payload.get("campaign_id") == self.spec.campaign_id
            and payload.get("candidate_id") == runtime.candidate_id
            and candidate_ref == runtime.source_ref
            and payload.get("phase") == phase.value
            and payload.get("task_set_digest") == task_set.fingerprint_digest
            and bundle.campaign_id == self.spec.campaign_id
            and bundle.candidate_id == runtime.candidate_id
            and bundle.phase is phase
            and bundle.task_set == task_set
            and bundle.baseline == self.spec.baseline
            and bundle.environment_digest == self.spec.environment.image_digest
            and bundle.solver_digest == self.spec.solver.model_digest
            and bundle.tools_digest == self.spec.tools_digest
            and bundle.permissions_digest == self.spec.permissions_digest
            and bundle.evaluator_digest == self.spec.evaluator.digest
            and bundle.status is ExperimentStatus.COMPLETED
        )
        if not expected:
            return _runtime_error(
                "EXPERIMENT_CHECKPOINT_MISMATCH",
                "Experiment checkpoint does not match the frozen campaign conditions",
                candidate_id=runtime.candidate_id,
                phase=phase.value,
            )
        return ok(experiment_ref)

    def _experiment_summary(self, refs: tuple[ArtifactRef, ...]) -> Result:
        totals = {
            "task_side_runs": 0,
            "infrastructure_retry_task_side_runs": 0,
            "solver_tokens": 0,
            "cost": Decimal("0"),
            "wall_time_seconds": 0,
        }
        experiments = []
        for ref in refs:
            loaded = load_experiment_bundle(self.store, ref)
            if not loaded.ok:
                return loaded
            bundle = loaded.value
            totals["task_side_runs"] += bundle.usage.task_side_runs
            totals["infrastructure_retry_task_side_runs"] += bundle.usage.infrastructure_retry_task_side_runs
            totals["solver_tokens"] += bundle.usage.solver_tokens
            totals["cost"] += Decimal(bundle.usage.cost)
            totals["wall_time_seconds"] += bundle.usage.wall_time_seconds
            experiments.append(
                {
                    "experiment_id": bundle.experiment_id,
                    "candidate_id": bundle.candidate_id,
                    "status": bundle.status.value,
                    "metrics": tuple(asdict(metric) for metric in bundle.metrics),
                }
            )
        usage = {**totals, "cost": str(totals["cost"])}
        return ok({"usage": usage, "experiments": tuple(experiments)})

    async def _candidate(self, candidate_id: str) -> Result:
        existing = self.candidates.get(candidate_id)
        if existing is not None:
            return ok(existing)
        events = await self.store.events(self.spec.campaign_id)
        if not events.ok:
            return events
        source_ref = None
        for event in reversed(events.value):
            if event.event_type == "candidate.created" and event.payload.get("candidate_id") == candidate_id:
                source_ref = _artifact_ref(event.payload["candidate_ref"])
                break
        if source_ref is None:
            return _runtime_error("CANDIDATE_NOT_FOUND", "Candidate source artifact is unavailable", candidate_id=candidate_id)
        read = self.store.artifacts.read_bytes(source_ref, expected_schema="loom.candidate-bundle.v1")
        if not read.ok:
            return read
        try:
            payload = json.loads(read.value)
            patch_ref = _artifact_ref(payload["patch"])
            patch = json.loads(self.store.artifacts.read_bytes(patch_ref).unwrap())
            surface = str(payload["changed_surfaces"][0])
            evidence_refs = tuple(str(value) for value in payload["evidence_refs"])
            compiled = self.compiler.compile(
                patch["base"],
                tuple(patch["operations"]),
                self.spec.candidate_policy,
                evidence_trace_ids=evidence_refs,
            ).unwrap()
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return _runtime_error("CANDIDATE_RESTORE_FAILED", "Candidate runtime could not be restored", cause=exc)
        restored = _CandidateRuntime(candidate_id, surface, source_ref, compiled, _task_harness(compiled.materialized))
        self.candidates[candidate_id] = restored
        return ok(restored)

    async def _optimization_stage_output(self, name: str) -> Result:
        store = SQLiteOptimizationStore(self.root)
        state = await store.load(self.root.name)
        if not state.ok:
            return state
        value = state.value.outputs.get(f"stage.{name}")
        if not isinstance(value, Mapping):
            return _runtime_error("OPTIMIZATION_REPLAY_INVALID", "Required optimization stage output is unavailable", stage=name)
        return ok(thaw_json(value))


def build_default_runtime(options) -> Result:
    if options.trace is None or options.config is None:
        return _runtime_error("VALIDATION_FAILED", "Optimize trace and config are required")
    trace_path = options.trace.resolve()
    if not trace_path.is_file():
        return _runtime_error("VALIDATION_FAILED", "Seed trace must be an existing file", trace_path=str(trace_path))
    loaded = load_optimize_config(options.config)
    if not loaded.ok:
        return loaded
    source_key = canonical_digest(
        {
            "trace": _file_digest(trace_path),
            "config": _file_digest(options.config),
            "tasks": _task_sources(options),
        }
    )
    staging = options.output_dir.resolve() / ".preflight" / source_key[:24]
    first = _prepare_tasks(options, loaded.value, staging)
    if not first.ok:
        return first
    model_digests = {
        role: canonical_digest(loaded.value.task_config.models[getattr(loaded.value.meta, f"{role}_model")]) for role in ("proposer", "solver", "judge")
    }
    optimization_key = canonical_digest(
        {
            "schema_version": "loom.optimization-key.v1",
            "trace_digest": _file_digest(trace_path),
            "config_digest": _file_digest(options.config),
            "task_set_digests": first.value.fingerprint_digests,
            "model_digests": model_digests,
            "search": loaded.value.meta.search,
            "objectives": loaded.value.meta.objectives,
            "execution": loaded.value.meta.execution,
        }
    )
    optimization_id = f"opt_{optimization_key[:24]}"
    root = options.output_dir.resolve() / optimization_id
    if options.new_run:
        fresh = _new_run_id(options.output_dir.resolve(), optimization_id, optimization_key)
        if not fresh.ok:
            return fresh
        optimization_id = fresh.value
        root = options.output_dir.resolve() / optimization_id
    prepared = _prepare_tasks(options, loaded.value, root)
    if not prepared.ok:
        return prepared
    governance = bootstrap_local_governance(root) if loaded.value.meta.governance.mode == "local" else load_production_governance()
    if not governance.ok:
        return governance
    campaign_actors = _campaign_actors(optimization_key, governance.value)
    identities = StaticIdentityProvider(
        (
            campaign_actors.creator,
            campaign_actors.operator,
            governance.value.controller,
            governance.value.finalizer,
        )
    )
    campaign_store = SQLiteCampaignStore(root / "campaign", identities)
    published = publish_prepared_task_sets(prepared.value, campaign_store.artifacts)
    if not published.ok:
        return published
    baseline = campaign_store.artifacts.publish_bytes(
        canonical_json_bytes(_baseline_harness(loaded.value)),
        kind="baseline_harness",
        schema_version="loom.baseline-harness.v1",
        suffix=".json",
    )
    if not baseline.ok:
        return baseline
    campaign_id = f"cmp_{optimization_key[:24]}"
    campaign_spec = _campaign_spec(
        loaded.value,
        campaign_id,
        baseline.value,
        published.value,
        prepared.value,
        model_digests,
        trace_path,
    )
    providers = {}
    for role in ("proposer", "solver", "judge"):
        selected = create_provider_from_task_config(
            loaded.value.task_config,
            model_name=getattr(loaded.value.meta, f"{role}_model"),
        )
        if not selected.ok:
            return selected
        providers[role] = selected.value
    spec = OptimizationSpec(
        "loom.optimization.spec.v1",
        optimization_id,
        optimization_key,
        trace_path,
        _file_digest(options.config),
        prepared.value.fingerprint_digests,
        model_digests,
        campaign_id,
    )
    optimization_store = SQLiteOptimizationStore(root)
    services = DefaultOptimizeCampaignServices(
        loaded=loaded.value,
        root=root,
        trace_path=trace_path,
        prepared=prepared.value,
        published=published.value,
        campaign_store=campaign_store,
        campaign_spec=campaign_spec,
        campaign_actors=campaign_actors,
        governance_actors=governance.value,
        proposer_provider=providers["proposer"],
        solver_provider=providers["solver"],
        judge_provider=providers["judge"],
    )
    observer = create_observer(tui=options.tui, json_output=options.json, stdout=sys.stdout, stderr=sys.stderr)
    if not observer.ok:
        return observer
    orchestrator = OptimizeOrchestrator(
        spec,
        optimization_store,
        services.contract(),
        iterations=loaded.value.meta.search.iterations,
        observer=observer.value,
        lease_seconds=max(60, loaded.value.meta.execution.trial_timeout_seconds * 2),
        governance=services.govern,
        output_dir=root,
    )
    return ok(DefaultOptimizeRuntime(orchestrator, services, prepared.value))


async def approve_default_runtime(options) -> Result:
    root = options.output_dir.resolve() / str(options.optimization_id)
    request_path = root / "approval-request.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _runtime_error("APPROVAL_CONTEXT_MISSING", "Approval context is unavailable or malformed", cause=exc)
    if request.get("candidate_id") != options.candidate_id:
        return _runtime_error("APPROVAL_CANDIDATE_MISMATCH", "Approval candidate does not match the pending request")
    mode = str(request.get("governance_mode", "local"))
    actors = bootstrap_local_governance(root) if mode == "local" else load_production_governance()
    if not actors.ok:
        return actors
    if options.identity not in {"approver", actors.value.approver.subject}:
        return _runtime_error("AUTHORIZATION_FAILED", "Approval identity does not match the configured approver")
    artifacts = ArtifactStore(root / "campaign" / "artifacts")
    restored = _restore_governance_inputs(request.get("governance_inputs"), artifacts)
    if not restored.ok:
        return restored
    decision_ref_value = request.get("decision_ref")
    try:
        decision_ref = _artifact_ref(decision_ref_value)
    except (TypeError, ValueError) as exc:
        return _runtime_error("APPROVAL_CONTEXT_INVALID", "Pending decision reference is invalid", cause=exc)
    decision_raw = artifacts.read_bytes(decision_ref, expected_schema="loom.promotion-decision.v1")
    if not decision_raw.ok:
        return decision_raw
    pending = _promotion_decision(decision_raw.value)
    if not pending.ok:
        return pending
    composer = OptimizeGovernanceComposer(artifacts, root / "governance", actors.value)
    recorded = await composer.record_approval(restored.value, pending.value, rationale=options.reason)
    if not recorded.ok:
        return recorded
    promoted = await composer.promote(restored.value)
    if not promoted.ok:
        return promoted
    if promoted.value.decision.decision is not PromotionDisposition.PROMOTED or promoted.value.monitor is None:
        return _runtime_error("APPROVAL_PROMOTION_FAILED", "Approved candidate did not reach promotion")
    governance_output = {
        "disposition": "promoted",
        "candidate_id": promoted.value.decision.candidate_id,
        "promotion_decision_ref": asdict(promoted.value.decision_ref),
        "monitor_ref": promoted.value.monitor,
    }
    store = SQLiteOptimizationStore(root)
    completed = await store.complete_approval(str(options.optimization_id), governance_output)
    if not completed.ok:
        return completed
    state = await store.load(str(options.optimization_id))
    if not state.ok:
        return state
    summary = {
        "schema_version": "loom.optimization.result.v1",
        "optimization_id": str(options.optimization_id),
        "campaign_id": restored.value.campaign_id,
        "disposition": "promoted",
        "candidate_id": promoted.value.decision.candidate_id,
        "promotion_decision_ref": asdict(promoted.value.decision_ref),
        "monitor_ref": promoted.value.monitor,
        "stages": {key.removeprefix("stage."): thaw_json(value) for key, value in state.value.outputs.items() if key.startswith("stage.")},
        "next_action": "No manual action required.",
    }
    report_path = write_report(root, summary)
    result = OptimizationResult(
        "loom.optimization.result.v1",
        str(options.optimization_id),
        restored.value.campaign_id,
        "promoted",
        promoted.value.decision.candidate_id,
        asdict(promoted.value.decision_ref),
        promoted.value.monitor,
        report_path,
    )
    write_result(root, result)
    exported = await store.export(str(options.optimization_id))
    return exported if not exported.ok else ok(result)


def _prepare_tasks(options, loaded: LoadedOptimizeConfig, root: Path) -> Result:
    if options.tasks is not None:
        return prepare_task_sets(
            options.tasks,
            config=loaded.meta.tasks,
            output_dir=root,
            owner="loom.optimize",
            visible_snapshot_digests=(_seed_workspace_digest(options.trace),) if _seed_workspace_digest(options.trace) else (),
        )
    if options.discovery_tasks is None or options.validation_tasks is None or options.holdout_tasks is None:
        return _runtime_error("VALIDATION_FAILED", "Explicit task-set paths are incomplete")
    return prepare_explicit_task_sets(
        options.discovery_tasks,
        options.validation_tasks,
        options.holdout_tasks,
        config=loaded.meta.tasks,
        output_dir=root,
        owner="loom.optimize",
    )


def _campaign_spec(
    loaded: LoadedOptimizeConfig,
    campaign_id: str,
    baseline: ArtifactRef,
    task_sets: PublishedTaskSets,
    prepared: PreparedTaskSets,
    model_digests: Mapping[str, str],
    trace_path: Path,
) -> CampaignSpec:
    meta = loaded.meta
    maximum_cost = meta.budgets.max_cost_usd if meta.budgets.max_cost_usd is not None else Decimal("1000000000")
    maximum_candidates = meta.search.iterations * meta.search.candidates_per_iteration

    def side_runs(task_set: PreparedTaskSet) -> int:
        return len(task_set.tasks) * meta.tasks.repetitions * 2 * maximum_candidates

    policy = CandidatePolicy(
        kinds=tuple(CandidateKind(value) for value in meta.search.candidate_kinds),
        editable_surfaces=meta.search.editable_surfaces,
        forbidden_surfaces=_FORBIDDEN_SURFACES,
        allowed_patch_operations=("set", "set_limit", "replace", "append_rule"),
        executable_enabled=False,
    )
    created_at = datetime.fromtimestamp(trace_path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return CampaignSpec(
        "loom.campaign.spec.v1",
        campaign_id,
        created_at,
        None,
        "Optimize the configured Loom task harness under frozen paired evidence.",
        baseline,
        SolverSpec(meta.solver_model, model_digests["solver"], {}),
        ProposerSpec("loom_native", meta.proposer_model, {}),
        EvaluatorSpec(
            "loom.agent-task.v1",
            meta.judge_model,
            model_digests["judge"],
            repetitions=meta.tasks.repetitions,
        ),
        EnvironmentSpec(canonical_digest({"runtime": "local", "version": "v1"}), "loom.optimize.v1", "fresh_snapshot"),
        canonical_digest({"tools": _ALL_TASK_TOOLS}),
        canonical_digest({"permissions": "workspace_only"}),
        meta.search.editable_surfaces,
        _FORBIDDEN_SURFACES,
        task_sets.discovery,
        task_sets.validation,
        task_sets.holdout,
        _objective_specs(meta.objectives, minimum_pairs=meta.tasks.minimum_pairs),
        CampaignBudget(
            meta.search.iterations,
            meta.search.candidates_per_iteration,
            (
                PhaseBudget(ExperimentPhase.DISCOVERY, maximum_candidates, side_runs(prepared.discovery)),
                PhaseBudget(ExperimentPhase.VALIDATION, maximum_candidates, side_runs(prepared.validation)),
                PhaseBudget(ExperimentPhase.HOLDOUT, maximum_candidates, side_runs(prepared.holdout)),
            ),
            maximum_candidates * 2,
            meta.budgets.max_llm_calls * 100_000,
            meta.budgets.max_llm_calls * 100_000,
            maximum_cost,
            meta.budgets.max_wall_clock_minutes * 60,
        ),
        policy,
        HistoryVisibilityPolicy(
            ("surface", "message", "details", "findings", "metrics"),
            ("event_type", "excerpt"),
        ),
        PromotionPolicy(
            primary_objective=meta.objectives.primary,
            minimum_improvement=0.0,
        ),
        MonitoringPolicy(20, max(1, meta.tasks.minimum_pairs), meta.objectives.max_regression_rate),
    )


def _baseline_harness(loaded: LoadedOptimizeConfig) -> dict[str, Any]:
    solver = loaded.task_config.models[loaded.meta.solver_model]
    return {
        "agent.system_prompt": {"system_prompt_addendum": ""},
        "agent.tool_policy": {"allowed_tools": list(_ALL_TASK_TOOLS)},
        "agent.loop_policy": {"max_tool_calls_per_step": 5, "max_history_steps": 5},
        "models.solver.request_options": dict(solver.request_options),
    }


def _objective_specs(config, *, minimum_pairs: int) -> tuple[ObjectiveSpec, ...]:
    return (
        ObjectiveSpec(
            "task_success_rate",
            ObjectiveDirection.MAXIMIZE,
            hard=True,
            max_baseline_regression=config.max_regression_rate,
            min_valid_pairs=minimum_pairs,
            missing_policy="fail_closed",
        ),
        ObjectiveSpec(
            "total_tokens",
            ObjectiveDirection.MINIMIZE,
            aggregation="relative_change",
            hard=True,
            max_baseline_regression=config.max_cost_increase_ratio,
            min_valid_pairs=minimum_pairs,
            missing_policy="fail_closed",
        ),
        ObjectiveSpec(
            "wall_time_ms",
            ObjectiveDirection.MINIMIZE,
            aggregation="relative_change",
            hard=True,
            max_baseline_regression=config.max_latency_increase_ratio,
            min_valid_pairs=minimum_pairs,
            missing_policy="fail_closed",
        ),
    )


def _surface_definitions(base: Mapping[str, Any]) -> tuple[SurfaceDefinition, ...]:
    request_options = base["models.solver.request_options"]
    return (
        SurfaceDefinition("agent.system_prompt", ("system_prompt_addendum",), {"system_prompt_addendum": str}),
        SurfaceDefinition("agent.tool_policy", ("allowed_tools",), {"allowed_tools": list}),
        SurfaceDefinition(
            "agent.loop_policy",
            ("max_tool_calls_per_step", "max_history_steps"),
            {"max_tool_calls_per_step": int, "max_history_steps": int},
            {"max_tool_calls_per_step": (1, 64), "max_history_steps": (1, 100)},
        ),
        SurfaceDefinition(
            "models.solver.request_options",
            tuple(request_options),
            {key: type(value) for key, value in request_options.items()},
        ),
    )


def _task_harness(materialized: Mapping[str, Any]) -> TaskHarness:
    prompt = materialized["agent.system_prompt"]
    tools = materialized["agent.tool_policy"]
    loop = materialized["agent.loop_policy"]
    request_options = materialized["models.solver.request_options"]
    return TaskHarness(
        system_prompt_addendum=str(prompt["system_prompt_addendum"]),
        allowed_tools=tuple(tools["allowed_tools"]),
        max_tool_calls_per_step=int(loop["max_tool_calls_per_step"]),
        max_history_steps=int(loop["max_history_steps"]),
        request_options=thaw_json(request_options),
    )


def _campaign_actors(key: str, governance) -> _CampaignActors:
    issued = "2026-01-01T00:00:00.000000Z"
    expires = "2999-01-01T00:00:00.000000Z"

    def actor(role: str) -> ActorAssertion:
        return ActorAssertion(
            f"optimize-{role}",
            (role,),
            issued,
            expires,
            f"optimize-{canonical_digest({'key': key, 'role': role})}",
        )

    return _CampaignActors(actor("campaign_creator"), actor("campaign_operator"), governance.controller, governance.finalizer)


def _task_sources(options) -> Mapping[str, str]:
    paths = {
        "tasks": options.tasks,
        "discovery": options.discovery_tasks,
        "validation": options.validation_tasks,
        "holdout": options.holdout_tasks,
    }
    return {name: _file_digest(path) for name, path in paths.items() if path is not None}


def _seed_workspace_digest(trace: Path | None) -> str:
    del trace
    return ""


def _valid_evolution_bundle(path: Path, source_evaluation_bundle: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        artifacts = payload["artifacts"]
        root = path.parent.resolve()
        referenced = tuple((root / str(artifacts[name])).resolve() for name in ("scores", "signals", "proposals", "report"))
        return (
            payload.get("schema_version") == "loom.evolution.bundle.v1"
            and isinstance(payload.get("bundle_id"), str)
            and bool(payload["bundle_id"])
            and Path(str(payload.get("source_evaluation_bundle"))).resolve() == source_evaluation_bundle.resolve()
            and all(item.is_relative_to(root) and item.is_file() for item in referenced)
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _new_run_id(output_dir: Path, base_id: str, optimization_key: str) -> Result:
    matching_run_exists = False
    for candidate in sorted(output_dir.glob(f"{base_id}*")):
        if candidate.name != base_id and not candidate.name.startswith(f"{base_id}_"):
            continue
        database = candidate / "optimization.sqlite"
        if not database.is_file():
            continue
        try:
            with sqlite3.connect(database) as connection:
                manifest = connection.execute("SELECT optimization_key FROM manifest WHERE singleton = 1").fetchone()
                if manifest is None or str(manifest[0]) != optimization_key:
                    continue
                matching_run_exists = True
                row = connection.execute("SELECT stage FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
                stage = None if row is None else str(row[0])
        except sqlite3.Error as exc:
            return _runtime_error("OPTIMIZATION_STORE_FAILED", "Existing optimization could not be inspected", cause=exc)
        if stage in {"holdout_complete", "governance_complete"}:
            return _runtime_error(
                "HOLDOUT_REUSE_FORBIDDEN",
                "A new optimization cannot reuse a disclosed holdout; change the task corpus or split seed",
            )
    if not matching_run_exists:
        return ok(base_id)
    created_at = utc_now()
    attempt = 0
    while True:
        suffix = canonical_digest({"optimization_key": optimization_key, "created_at": created_at, "attempt": attempt})[:8]
        fresh_id = f"{base_id}_{suffix}"
        if not (output_dir / fresh_id).exists():
            return ok(fresh_id)
        attempt += 1


def _governance_input_payload(inputs: GovernanceInputs) -> dict[str, Any]:
    return {
        "campaign_id": inputs.campaign_id,
        "candidate_id": inputs.candidate_id,
        "surface_id": inputs.surface_id,
        "source_candidate_ref": asdict(inputs.source_candidate_ref),
        "recommendation_ref": asdict(inputs.recommendation_ref),
        "baseline_ref": asdict(inputs.baseline_ref),
        "supporting_evidence": tuple(asdict(ref) for ref in inputs.supporting_evidence),
        "allowed_surfaces": inputs.allowed_surfaces,
        "minimum_improvement": inputs.minimum_improvement,
        "minimum_sample_size": inputs.minimum_sample_size,
        "regression_threshold": inputs.regression_threshold,
        "ttl_runs": inputs.ttl_runs,
        "heartbeat_grace_seconds": inputs.heartbeat_grace_seconds,
        "compiled": {
            "base": thaw_json(inputs.compiled.base),
            "operations": tuple(thaw_json(value) for value in inputs.compiled.operations),
        },
    }


def _restore_governance_inputs(value: Any, artifacts: ArtifactStore) -> Result:
    del artifacts
    if not isinstance(value, Mapping):
        return _runtime_error("APPROVAL_CONTEXT_INVALID", "Governance input context is missing")
    try:
        payload = thaw_json(value)
        compiled_payload = payload["compiled"]
        base = compiled_payload["base"]
        operations = tuple(compiled_payload["operations"])
        allowed_surfaces = tuple(payload["allowed_surfaces"])
        policy = CandidatePolicy(
            kinds=(CandidateKind.DECLARATIVE_PATCH,),
            editable_surfaces=allowed_surfaces,
            forbidden_surfaces=_FORBIDDEN_SURFACES,
            allowed_patch_operations=("set", "set_limit", "replace", "append_rule"),
        )
        compiled = (
            DeclarativePatchCompiler(_surface_definitions(base))
            .compile(
                base,
                operations,
                policy,
                evidence_trace_ids=("approval-context",),
            )
            .unwrap()
        )
        return ok(
            GovernanceInputs(
                str(payload["campaign_id"]),
                str(payload["candidate_id"]),
                str(payload["surface_id"]),
                _artifact_ref(payload["source_candidate_ref"]),
                _artifact_ref(payload["recommendation_ref"]),
                compiled,
                _artifact_ref(payload["baseline_ref"]),
                tuple(_artifact_ref(ref) for ref in payload["supporting_evidence"]),
                allowed_surfaces,
                float(payload["minimum_improvement"]),
                int(payload["minimum_sample_size"]),
                float(payload["regression_threshold"]),
                int(payload["ttl_runs"]),
                int(payload["heartbeat_grace_seconds"]),
            )
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return _runtime_error("APPROVAL_CONTEXT_INVALID", "Governance input context could not be restored", cause=exc)


def _promotion_decision(raw: bytes) -> Result:
    try:
        payload = json.loads(raw)
        gates = tuple(
            GateDecision(
                str(value["gate_id"]),
                bool(value["mandatory"]),
                GateResult(value["result"]),
                tuple(_artifact_ref(ref) for ref in value["evidence_refs"]),
                str(value["reason"]),
            )
            for value in payload["gates"]
        )
        approval = payload.get("human_approval")
        return ok(
            PromotionDecision(
                str(payload["schema_version"]),
                str(payload["decision_id"]),
                str(payload["campaign_id"]),
                str(payload["candidate_id"]),
                str(payload["created_at"]),
                str(payload["baseline_digest"]),
                str(payload["policy_digest"]),
                _artifact_ref(payload["risk_rule_set"]),
                gates,
                RiskLevel(payload["computed_risk"]),
                tuple(str(value) for value in payload["matched_risk_rules"]),
                None if approval is None else ApprovalRecord(**approval),
                PromotionDisposition(payload["decision"]),
                None if payload.get("active_artifact") is None else _artifact_ref(payload["active_artifact"]),
                None if payload.get("rollback_artifact") is None else _artifact_ref(payload["rollback_artifact"]),
            )
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _runtime_error("APPROVAL_CONTEXT_INVALID", "Pending promotion decision is invalid", cause=exc)


def _artifact_ref(value: Any) -> ArtifactRef:
    if isinstance(value, ArtifactRef):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("artifact reference must be a mapping")
    return ArtifactRef(**thaw_json(value))


def _file_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _operation_id(kind: str, identity: Any) -> str:
    return prefixed_id_from_digest("op_", canonical_digest({"kind": kind, "identity": identity}))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _runtime_error(code: str, message: str, *, cause: BaseException | None = None, **metadata: Any) -> Result:
    return err(
        make_loom_error(
            code,
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
            metadata=metadata,
        )
    )


__all__ = ["DefaultOptimizeRuntime", "build_default_runtime"]
