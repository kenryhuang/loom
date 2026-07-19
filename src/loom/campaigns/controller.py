"""Campaign state-machine operations for declarative search and finalization."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from loom.campaigns.contracts import (
    ArtifactRef,
    BudgetUsage,
    CampaignLifecycle,
    CampaignSpec,
    CandidateLifecycle,
    ExperimentPhase,
    ExperimentStatus,
    ObjectiveDirection,
)
from loom.campaigns.experiments import freeze_trial_plan, load_experiment_bundle, objective_gate_result
from loom.campaigns.frontier import CandidateObjectives, build_frontier_snapshot
from loom.campaigns.operations import BudgetReservation, CampaignOperation
from loom.campaigns.proposer import ProposalBatch, ProposalRequest
from loom.campaigns.reports import publish_promotion_recommendation
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, prefixed_id_from_digest
from loom.campaigns.validation import load_validation_evidence
from loom.core import Result, err, make_loom_error, ok, thaw_json


@dataclass(frozen=True, slots=True)
class SealedSearch:
    frontier_digest: str
    validation_entrants: tuple[str, ...]
    entrant_digest: str


@dataclass(frozen=True, slots=True)
class FinalistSelection:
    candidate_ids: tuple[str, ...]
    finalist_digest: str


@dataclass(frozen=True, slots=True)
class PromotionRecommendation:
    schema_version: str
    campaign_id: str
    candidate_id: str | None
    disposition: str
    report_ref: ArtifactRef
    active_artifact: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class VerifiedPhaseResults:
    results: dict[str, dict[str, Any]]
    experiment_refs: tuple[ArtifactRef, ...]
    task_side_runs: int


class CampaignController:
    def __init__(self, store, spec: CampaignSpec, *, controller_actor, finalizer_actor):
        self.store = store
        self.spec = spec
        self.controller_actor = controller_actor
        self.finalizer_actor = finalizer_actor
        self._scores: dict[str, dict[str, Any]] = {}
        self._objectives: dict[str, CandidateObjectives] = {}
        self._experiment_created_at: dict[str, str] = {}
        self._frontier_ids: frozenset[str] = frozenset()
        self._sealed: SealedSearch | None = None
        self._finalists: FinalistSelection | None = None
        self._restored = False

    async def restore(self) -> Result:
        """Rebuild all controller-owned decisions from the verified event log."""
        events = await self.store.events(self.spec.campaign_id)
        if not events.ok:
            return events
        scores: dict[str, dict[str, Any]] = {}
        objectives: dict[str, CandidateObjectives] = {}
        experiment_created_at: dict[str, str] = {}
        sealed = None
        finalists = None
        for event in events.value:
            payload = event.payload
            if event.event_type == "experiment.completed":
                candidate_id = payload.get("candidate_id")
                score = payload.get("score")
                if isinstance(candidate_id, str) and isinstance(score, Mapping):
                    scores[candidate_id] = dict(thaw_json(score))
                    ref_payload = payload.get("experiment_ref")
                    if isinstance(ref_payload, Mapping):
                        try:
                            bundle = load_experiment_bundle(self.store, ArtifactRef(**ref_payload))
                        except (TypeError, ValueError):
                            return _controller_error("Persisted experiment reference is invalid")
                        if not bundle.ok:
                            return bundle
                        objectives[candidate_id] = CandidateObjectives(candidate_id, bundle.value.metrics)
                        experiment_created_at[candidate_id] = bundle.value.created_at
            elif event.event_type == "campaign.search_sealed":
                entrants = payload.get("validation_entrants")
                if isinstance(entrants, tuple | list):
                    sealed = SealedSearch(
                        str(payload.get("frontier_digest", "")),
                        tuple(str(item) for item in entrants),
                        str(payload.get("entrant_digest", "")),
                    )
            elif event.event_type == "campaign.finalists_selected":
                candidate_ids = payload.get("candidate_ids")
                if isinstance(candidate_ids, tuple | list):
                    finalists = FinalistSelection(
                        tuple(str(item) for item in candidate_ids),
                        str(payload.get("finalist_digest", "")),
                    )
        self._scores = scores
        self._objectives = objectives
        self._experiment_created_at = experiment_created_at
        self._frontier_ids = self._calculate_frontier_ids()
        self._sealed = sealed
        self._finalists = finalists
        self._restored = True
        return ok(self)

    async def _ensure_restored(self) -> Result:
        return ok(self) if self._restored else await self.restore()

    async def record_candidate(
        self,
        candidate_id: str,
        *,
        candidate_ref: ArtifactRef,
        validation_ref: ArtifactRef,
        experiment_ref: ArtifactRef | None,
    ) -> Result:
        restored = await self._ensure_restored()
        if not restored.ok:
            return restored
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        if projection.value.lifecycle is not CampaignLifecycle.RUNNING:
            return _controller_error("Campaign search is closed")
        validation = load_validation_evidence(
            self.store,
            validation_ref,
            campaign_id=self.spec.campaign_id,
            candidate_id=candidate_id,
            policy_digest=canonical_digest(self.spec.candidate_policy),
        )
        if not validation.ok:
            return validation
        valid, validated_candidate_ref, _ = validation.value
        if validated_candidate_ref != candidate_ref:
            return _controller_error("Validation evidence is bound to another candidate artifact")
        score: dict[str, Any] = {}
        experiment = None
        if valid:
            if experiment_ref is None:
                return _controller_error("Validated candidate requires a discovery ExperimentBundle")
            experiment = _load_authoritative_experiment(
                self.store,
                self.spec,
                experiment_ref,
                candidate_id=candidate_id,
                phase=ExperimentPhase.DISCOVERY,
            )
            if not experiment.ok:
                return experiment
            score = _experiment_score(self.spec, experiment.value)
        elif experiment_ref is not None:
            return _controller_error("Invalid candidate cannot attach discovery experiment evidence")
        existing = projection.value.candidates.get(candidate_id)
        if existing is None:
            created = await self.store.transact(
                CampaignOperation(
                    _stable_operation_id("candidate.created", candidate_id),
                    self.spec.campaign_id,
                    canonical_digest({"candidate_id": candidate_id, "event": "created", "candidate_ref": candidate_ref}),
                    "candidate.created",
                    actor=self.controller_actor,
                    payload={"candidate_id": candidate_id, "candidate_ref": candidate_ref},
                    output_refs=(candidate_ref,),
                    reservation=BudgetReservation(ExperimentPhase.DISCOVERY, candidates=1),
                ),
                projection.value.aggregate_version,
            )
            if not created.ok:
                return created
            version = created.value.aggregate_version
        else:
            if existing.lifecycle is not CandidateLifecycle.PROPOSED:
                return _controller_error("Candidate has already entered validation")
            events = await self.store.events(self.spec.campaign_id)
            if not events.ok:
                return events
            admitted_ref = next(
                (
                    ArtifactRef(**event.payload["candidate_ref"])
                    for event in reversed(events.value)
                    if event.event_type == "candidate.created"
                    and event.payload.get("candidate_id") == candidate_id
                    and isinstance(event.payload.get("candidate_ref"), Mapping)
                ),
                None,
            )
            if admitted_ref != candidate_ref:
                return _controller_error("Candidate artifact does not match the manually admitted bundle")
            version = projection.value.aggregate_version
        validated = await self.store.transact(
            CampaignOperation(
                _stable_operation_id("candidate.validation.completed", candidate_id),
                self.spec.campaign_id,
                canonical_digest(
                    {
                        "candidate_id": candidate_id,
                        "candidate_ref": candidate_ref,
                        "validation_ref": validation_ref,
                        "valid": valid,
                    }
                ),
                "candidate.validation.completed",
                actor=self.controller_actor,
                payload={"candidate_id": candidate_id, "valid": valid, "evidence_ref": validation_ref},
                output_refs=(validation_ref,),
            ),
            version,
        )
        if not validated.ok or not valid:
            return validated
        evaluated = await self.store.transact(
            CampaignOperation(
                _stable_operation_id("experiment.completed", candidate_id),
                self.spec.campaign_id,
                canonical_digest({"candidate_id": candidate_id, "score": score, "experiment_ref": experiment_ref}),
                "experiment.completed",
                actor=self.controller_actor,
                payload={"candidate_id": candidate_id, "score": score, "experiment_ref": experiment_ref},
                output_refs=(experiment_ref,),
                reservation=BudgetReservation(
                    ExperimentPhase.DISCOVERY,
                    candidate_experiments=1,
                    task_side_runs=_non_negative_int(score.get("task_side_runs", 0)),
                    solver_tokens=_non_negative_int(score.get("solver_tokens", 0)),
                    cost=str(score.get("cost", "0")),
                    wall_time_seconds=_non_negative_int(score.get("wall_time_seconds", 0)),
                ),
            ),
            validated.value.aggregate_version,
        )
        if evaluated.ok:
            self._scores[candidate_id] = dict(score)
            self._objectives[candidate_id] = CandidateObjectives(candidate_id, experiment.value.metrics)
            self._experiment_created_at[candidate_id] = experiment.value.created_at
            self._frontier_ids = self._calculate_frontier_ids()
        return evaluated

    async def run_iteration(
        self,
        iteration: int,
        proposer,
        history,
        workspace,
        evaluate_draft,
    ) -> Result:
        restored = await self._ensure_restored()
        if not restored.ok:
            return restored
        if iteration < 1 or iteration > self.spec.budget.iterations:
            return _controller_error("Campaign iteration is outside its frozen budget", iteration=iteration)
        events = await self.store.events(self.spec.campaign_id)
        if not events.ok:
            return events
        for event in events.value:
            if event.event_type == "campaign.iteration_completed" and event.payload.get("iteration") == iteration:
                return ok(tuple(event.payload.get("candidate_ids", ())))
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        if projection.value.lifecycle is not CampaignLifecycle.RUNNING:
            return _controller_error("Campaign must be running before starting an iteration")
        usage = await self.store.budget_usage(self.spec.campaign_id)
        if not usage.ok:
            return usage
        proposer_tokens = max(
            0,
            self.spec.budget.proposer_tokens - usage.value.proposer_tokens - usage.value.reserved_proposer_tokens,
        )
        cost = format(
            max(
                Decimal("0"),
                self.spec.budget.maximum_cost - Decimal(usage.value.cost) - Decimal(usage.value.reserved_cost),
            ),
            "f",
        )
        wall_time_seconds = max(
            0,
            self.spec.budget.wall_time_seconds - usage.value.wall_time_seconds - usage.value.reserved_wall_time_seconds,
        )
        started = await self.store.transact(
            CampaignOperation(
                _stable_operation_id("campaign.iteration_started", iteration),
                self.spec.campaign_id,
                canonical_digest(
                    {
                        "iteration": iteration,
                        "proposer": self.spec.proposer,
                        "proposer_tokens": proposer_tokens,
                        "cost": cost,
                        "wall_time_seconds": wall_time_seconds,
                    }
                ),
                "campaign.iteration_started",
                actor=self.controller_actor,
                payload={"iteration": iteration},
                reservation=BudgetReservation(
                    ExperimentPhase.DISCOVERY,
                    iterations=1,
                    proposer_tokens=proposer_tokens,
                    cost=cost,
                    wall_time_seconds=wall_time_seconds,
                ),
                complete=False,
                lease_seconds=max(60, self.spec.budget.wall_time_seconds),
            ),
            projection.value.aggregate_version,
        )
        if not started.ok:
            return started
        request = ProposalRequest(
            self.spec.campaign_id,
            iteration,
            self.spec.budget.candidates_per_iteration,
            {
                "proposer_tokens": proposer_tokens,
                "cost": cost,
                "wall_time_seconds": wall_time_seconds,
            },
            self.spec.editable_surfaces,
            self.spec.forbidden_surfaces,
        )
        proposed = await proposer.propose(request, history, workspace)
        if not proposed.ok:
            await self.store.cancel_lease(started.value.lease_id, self.controller_actor)
            return proposed
        if not isinstance(proposed.value, ProposalBatch) or len(proposed.value.drafts) > request.max_candidates:
            await self.store.cancel_lease(started.value.lease_id, self.controller_actor)
            return _controller_error("Proposer returned an invalid measured proposal batch")
        actual = BudgetReservation(
            ExperimentPhase.DISCOVERY,
            iterations=1,
            proposer_tokens=proposed.value.usage.proposer_tokens,
            cost=proposed.value.usage.cost,
            wall_time_seconds=proposed.value.usage.wall_time_seconds,
        )
        completed = await self.store.complete_lease(started.value.lease_id, self.controller_actor, actual)
        if not completed.ok:
            return completed
        candidate_ids = []
        for draft in proposed.value.drafts:
            candidate_id = prefixed_id_from_digest("cand_", canonical_digest(draft))
            evaluated = evaluate_draft(draft, candidate_id)
            evaluated = await evaluated if inspect.isawaitable(evaluated) else evaluated
            if not evaluated.ok:
                return evaluated
            candidate_ref, validation_ref, experiment_ref = evaluated.value
            recorded = await self.record_candidate(
                candidate_id,
                candidate_ref=candidate_ref,
                validation_ref=validation_ref,
                experiment_ref=experiment_ref,
            )
            if not recorded.ok:
                return recorded
            candidate_ids.append(candidate_id)
        projection = await self.store.load(self.spec.campaign_id)
        committed = await self.store.transact(
            CampaignOperation(
                _stable_operation_id("campaign.iteration_completed", iteration),
                self.spec.campaign_id,
                canonical_digest({"iteration": iteration, "candidate_ids": candidate_ids}),
                "campaign.iteration_completed",
                actor=self.controller_actor,
                payload={"iteration": iteration, "candidate_ids": candidate_ids},
            ),
            projection.value.aggregate_version,
        )
        return ok(tuple(candidate_ids)) if committed.ok else committed

    def frontier_digest(self) -> str:
        return canonical_digest({candidate_id: self._scores[candidate_id] for candidate_id in sorted(self._scores)})

    def _calculate_frontier_ids(self) -> frozenset[str]:
        if not self._objectives:
            return frozenset()
        snapshot = build_frontier_snapshot(
            self.spec.campaign_id,
            0,
            tuple(self._objectives.values()),
            self.spec.objectives,
            BudgetUsage(),
            created_at=min(self._experiment_created_at.values(), default=self.spec.created_at),
        )
        return frozenset(snapshot.candidate_ids)

    async def _persist_frontier(self, expected_version: int) -> Result:
        usage = await self.store.budget_usage(self.spec.campaign_id)
        if not usage.ok:
            return usage
        events = await self.store.events(self.spec.campaign_id)
        if not events.ok:
            return events
        iteration = sum(event.event_type == "campaign.iteration_completed" for event in events.value)
        consumed = usage.value
        snapshot = build_frontier_snapshot(
            self.spec.campaign_id,
            iteration,
            tuple(self._objectives.values()),
            self.spec.objectives,
            BudgetUsage(
                iterations=consumed.iterations,
                candidates=consumed.candidates,
                candidate_experiments=consumed.consumed_candidate_experiments,
                task_side_runs=consumed.consumed_task_side_runs,
                infrastructure_retry_task_side_runs=consumed.infrastructure_retry_task_side_runs,
                proposer_tokens=consumed.proposer_tokens,
                solver_tokens=consumed.solver_tokens,
                cost=consumed.cost,
                wall_time_seconds=consumed.wall_time_seconds,
            ),
            created_at=max(self._experiment_created_at.values(), default=self.spec.created_at),
        )
        published = self.store.artifacts.publish_bytes(
            canonical_json_bytes(snapshot),
            kind="frontier_snapshot",
            schema_version="loom.frontier.snapshot.v1",
            suffix=".json",
        )
        if not published.ok:
            return published
        return await self.store.transact(
            CampaignOperation(
                _stable_operation_id("frontier.updated", self.frontier_digest()),
                self.spec.campaign_id,
                canonical_digest({"snapshot": snapshot, "artifact_ref": published.value}),
                "frontier.updated",
                actor=self.controller_actor,
                payload={"artifact_digest": published.value.sha256, "artifact_ref": published.value},
                output_refs=(published.value,),
            ),
            expected_version,
        )

    async def seal_search(self, operation_id: str, expect_frontier_digest: str) -> Result:
        restored = await self._ensure_restored()
        if not restored.ok:
            return restored
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        if projection.value.lifecycle not in {CampaignLifecycle.RUNNING, CampaignLifecycle.PAUSED} or self._sealed is not None:
            return _controller_error("Campaign search is already sealed or unavailable")
        actual = self.frontier_digest()
        if expect_frontier_digest != actual:
            return _controller_error("Frontier digest changed", expected=expect_frontier_digest, actual=actual)
        persisted = await self._persist_frontier(projection.value.aggregate_version)
        if not persisted.ok:
            return persisted
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        limit = _phase_limit(self.spec, ExperimentPhase.VALIDATION)
        entrants = tuple(
            candidate_id
            for candidate_id, _ in sorted(
                (
                    (candidate_id, score)
                    for candidate_id, score in self._scores.items()
                    if candidate_id in self._frontier_ids and int(score.get("critical_regressions", 0)) == 0
                ),
                key=lambda item: (
                    -float(item[1].get("primary_lcb", float("-inf"))),
                    int(item[1].get("critical_regressions", 0)),
                    float(item[1].get("cost_ucb", float("inf"))),
                    item[0],
                ),
            )[:limit]
        )
        sealed = SealedSearch(actual, entrants, canonical_digest(entrants))
        committed = await self.store.transact(
            CampaignOperation(
                operation_id,
                self.spec.campaign_id,
                canonical_digest(sealed),
                "campaign.search_sealed",
                actor=self.finalizer_actor,
                payload={
                    "frontier_digest": actual,
                    "validation_entrants": entrants,
                    "entrant_digest": sealed.entrant_digest,
                },
            ),
            projection.value.aggregate_version,
        )
        if not committed.ok:
            return committed
        self._sealed = sealed
        return ok(sealed)

    async def select_finalists(self, validation_results_ref: ArtifactRef, *, dry_run: bool = False) -> Result:
        restored = await self._ensure_restored()
        if not restored.ok:
            return restored
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        if projection.value.lifecycle is not CampaignLifecycle.SEARCH_SEALED or self._sealed is None or self._finalists is not None:
            return _controller_error("Validation requires a sealed search")
        loaded = _load_phase_results(
            self.store,
            validation_results_ref,
            spec=self.spec,
            campaign_id=self.spec.campaign_id,
            phase="validation",
            cohort_digest=self._sealed.entrant_digest,
        )
        if not loaded.ok:
            return loaded
        evidence = loaded.value
        validation_results = evidence.results
        entrants = frozenset(self._sealed.validation_entrants)
        eligible = [(candidate_id, result) for candidate_id, result in validation_results.items() if candidate_id in entrants and result.get("passed") is True]
        limit = _phase_limit(self.spec, ExperimentPhase.HOLDOUT)
        candidate_ids = tuple(
            candidate_id
            for candidate_id, _ in sorted(
                eligible,
                key=lambda item: (
                    -float(item[1].get("primary_lcb", float("-inf"))),
                    int(item[1].get("critical_regressions", 0)),
                    float(item[1].get("cost_ucb", float("inf"))),
                    item[0],
                ),
            )[:limit]
        )
        selection = FinalistSelection(candidate_ids, canonical_digest(candidate_ids))
        if dry_run:
            return ok(selection)
        consumed = await self.store.transact(
            CampaignOperation(
                _stable_operation_id("campaign.validation_evidence_consumed", validation_results_ref.sha256),
                self.spec.campaign_id,
                canonical_digest({"phase": "validation", "experiment_refs": evidence.experiment_refs}),
                "campaign.validation_evidence_consumed",
                actor=self.finalizer_actor,
                payload={"evidence_ref": validation_results_ref},
                reservation=BudgetReservation(
                    ExperimentPhase.VALIDATION,
                    candidate_experiments=len(evidence.experiment_refs),
                    task_side_runs=evidence.task_side_runs,
                ),
            ),
            projection.value.aggregate_version,
        )
        if not consumed.ok:
            return consumed
        for entrant in self._sealed.validation_entrants:
            projection = await self.store.load(self.spec.campaign_id)
            if not projection.ok:
                return projection
            passed = validation_results.get(entrant, {}).get("passed") is True
            transition = await self.store.transact(
                CampaignOperation(
                    _stable_operation_id("candidate.validation.phase", (validation_results_ref.sha256, entrant)),
                    self.spec.campaign_id,
                    canonical_digest({"candidate_id": entrant, "passed": passed, "evidence_ref": validation_results_ref}),
                    "candidate.validation.passed" if passed else "candidate.validation.rejected",
                    actor=self.finalizer_actor,
                    payload={"candidate_id": entrant, "evidence_ref": validation_results_ref},
                    output_refs=(validation_results_ref,),
                ),
                projection.value.aggregate_version,
            )
            if not transition.ok:
                return transition
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        committed = await self.store.transact(
            CampaignOperation(
                _stable_operation_id("campaign.finalists_selected", selection.finalist_digest),
                self.spec.campaign_id,
                canonical_digest(selection),
                "campaign.finalists_selected",
                actor=self.finalizer_actor,
                payload={"candidate_ids": candidate_ids, "finalist_digest": selection.finalist_digest},
            ),
            projection.value.aggregate_version,
        )
        if not committed.ok:
            return committed
        self._finalists = selection
        return ok(selection)

    async def finalize(self, expect_finalist_digest: str, holdout_results_ref: ArtifactRef, *, operation_id: str) -> Result:
        restored = await self._ensure_restored()
        if not restored.ok:
            return restored
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        if projection.value.lifecycle is not CampaignLifecycle.SEARCH_SEALED or self._finalists is None:
            return _controller_error("Campaign holdout is unavailable or already finalized")
        if expect_finalist_digest != self._finalists.finalist_digest:
            return _controller_error("Finalist digest changed")
        loaded = _load_phase_results(
            self.store,
            holdout_results_ref,
            spec=self.spec,
            campaign_id=self.spec.campaign_id,
            phase="holdout",
            cohort_digest=self._finalists.finalist_digest,
        )
        if not loaded.ok:
            return loaded
        evidence = loaded.value
        holdout_results = evidence.results
        consumed = await self.store.transact(
            CampaignOperation(
                _stable_operation_id("campaign.holdout_evidence_consumed", holdout_results_ref.sha256),
                self.spec.campaign_id,
                canonical_digest({"phase": "holdout", "experiment_refs": evidence.experiment_refs}),
                "campaign.holdout_evidence_consumed",
                actor=self.finalizer_actor,
                payload={"evidence_ref": holdout_results_ref},
                reservation=BudgetReservation(
                    ExperimentPhase.HOLDOUT,
                    candidate_experiments=len(evidence.experiment_refs),
                    task_side_runs=evidence.task_side_runs,
                ),
            ),
            projection.value.aggregate_version,
        )
        if not consumed.ok:
            return consumed
        for finalist in self._finalists.candidate_ids:
            projection = await self.store.load(self.spec.campaign_id)
            if not projection.ok:
                return projection
            passed = holdout_results.get(finalist, {}).get("passed") is True
            transition = await self.store.transact(
                CampaignOperation(
                    _stable_operation_id("candidate.holdout.phase", (holdout_results_ref.sha256, finalist)),
                    self.spec.campaign_id,
                    canonical_digest({"candidate_id": finalist, "passed": passed, "evidence_ref": holdout_results_ref}),
                    "candidate.holdout.passed" if passed else "candidate.holdout.rejected",
                    actor=self.finalizer_actor,
                    payload={"candidate_id": finalist, "evidence_ref": holdout_results_ref},
                    output_refs=(holdout_results_ref,),
                ),
                projection.value.aggregate_version,
            )
            if not transition.ok:
                return transition
        candidate_id = next(
            (candidate_id for candidate_id in self._finalists.candidate_ids if holdout_results.get(candidate_id, {}).get("passed") is True),
            None,
        )
        candidate_artifact_ref = None
        if candidate_id is not None:
            events = await self.store.events(self.spec.campaign_id)
            if not events.ok:
                return events
            candidate_artifact_ref = next(
                (
                    ArtifactRef(**event.payload["candidate_ref"])
                    for event in reversed(events.value)
                    if event.event_type == "candidate.created"
                    and event.payload.get("candidate_id") == candidate_id
                    and isinstance(event.payload.get("candidate_ref"), Mapping)
                ),
                None,
            )
            if candidate_artifact_ref is None:
                return _controller_error("Recommended candidate has no immutable source artifact")
        disposition = "recommend" if candidate_id is not None else "do_not_recommend"
        report_payload = {
            "schema_version": "loom.promotion-recommendation.v1",
            "campaign_id": self.spec.campaign_id,
            "campaign_spec_digest": canonical_digest(self.spec),
            "candidate_id": candidate_id,
            "candidate_artifact_ref": candidate_artifact_ref,
            "baseline_ref": self.spec.baseline,
            "baseline_digest": self.spec.baseline.sha256,
            "promotion_policy_digest": canonical_digest(self.spec.promotion_policy),
            "task_set_digests": {
                "discovery": self.spec.discovery_set.fingerprint_digest,
                "validation": self.spec.validation_set.fingerprint_digest,
                "holdout": self.spec.holdout_set.fingerprint_digest,
            },
            "frontier_digest": self._sealed.frontier_digest if self._sealed is not None else None,
            "finalist_digest": self._finalists.finalist_digest,
            "holdout_results": holdout_results,
            "holdout_results_ref": holdout_results_ref,
            "risk_status": "computed_at_governance_boundary",
            "rollback_status": "required_before_activation",
            "disposition": disposition,
            "activates_registry": False,
        }
        report = publish_promotion_recommendation(self.store, report_payload, self.finalizer_actor)
        if not report.ok:
            return report
        report_ref, markdown_ref, signed_payload = report.value
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        committed = await self.store.transact(
            CampaignOperation(
                operation_id,
                self.spec.campaign_id,
                canonical_digest(signed_payload),
                "campaign.finalized",
                actor=self.finalizer_actor,
                payload={"report_ref": report_ref, "markdown_ref": markdown_ref, "finalist_digest": self._finalists.finalist_digest},
                output_refs=(report_ref, markdown_ref),
            ),
            projection.value.aggregate_version,
        )
        if not committed.ok:
            return committed
        return ok(PromotionRecommendation("loom.promotion-recommendation.v1", self.spec.campaign_id, candidate_id, disposition, report_ref))


def _phase_limit(spec: CampaignSpec, phase: ExperimentPhase) -> int:
    return next(item.max_candidate_experiments for item in spec.budget.phases if item.phase is phase)


def _stable_operation_id(kind: str, identity: Any) -> str:
    return prefixed_id_from_digest("op_", canonical_digest({"kind": kind, "identity": identity}))


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Experiment resource usage must be a non-negative integer")
    return value


def _controller_error(message: str, **metadata) -> Result:
    return err(make_loom_error("CAMPAIGN_OPERATION_INVALID", message, retryable=False, metadata=metadata))


def publish_phase_results(
    store,
    spec: CampaignSpec,
    phase: str,
    cohort_digest: str,
    experiment_refs: tuple[ArtifactRef, ...],
) -> Result:
    if phase not in {"validation", "holdout"}:
        return _controller_error("Phase results must be validation or holdout")
    role = ExperimentPhase(phase)
    derived = _derive_phase_results(store, spec, role, experiment_refs)
    if not derived.ok:
        return derived
    results = derived.value

    payload = {
        "schema_version": "loom.phase-results.v1",
        "campaign_id": spec.campaign_id,
        "phase": phase,
        "cohort_digest": cohort_digest,
        "results": results,
        "experiment_refs": experiment_refs,
    }
    return store.artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind=f"{phase}_results",
        schema_version="loom.phase-results.v1",
        suffix=".json",
    )


def _derive_phase_results(store, spec: CampaignSpec, role: ExperimentPhase, experiment_refs: tuple[ArtifactRef, ...]) -> Result:
    results: dict[str, dict[str, Any]] = {}
    seen = set()
    for ref in experiment_refs:
        loaded = load_experiment_bundle(store, ref)
        if not loaded.ok:
            return loaded
        bundle = loaded.value
        authoritative = _load_authoritative_experiment(store, spec, ref, candidate_id=bundle.candidate_id, phase=role)
        if not authoritative.ok:
            return authoritative
        if bundle.candidate_id in seen:
            return _controller_error("Phase result contains duplicate candidate experiments")
        seen.add(bundle.candidate_id)
        score = _experiment_score(spec, bundle)
        results[bundle.candidate_id] = {
            **score,
            "passed": score["critical_regressions"] == 0,
            "experiment_ref": ref,
        }
    return ok(results)


def _load_phase_results(store, ref: ArtifactRef, *, spec: CampaignSpec, campaign_id: str, phase: str, cohort_digest: str) -> Result:
    read = store.artifacts.read_bytes(ref)
    if not read.ok:
        return read
    try:
        import json

        payload = json.loads(read.value)
        if (
            payload.get("schema_version") != "loom.phase-results.v1"
            or payload.get("campaign_id") != campaign_id
            or payload.get("phase") != phase
            or payload.get("cohort_digest") != cohort_digest
            or not isinstance(payload.get("results"), dict)
            or not isinstance(payload.get("experiment_refs"), list)
        ):
            raise ValueError("phase result identity or cohort binding does not match")
        results = payload["results"]
        if not all(isinstance(candidate_id, str) and isinstance(value, dict) for candidate_id, value in results.items()):
            raise TypeError("phase result entries must be candidate mappings")
        experiment_refs = tuple(ArtifactRef(**item) for item in payload["experiment_refs"])
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Phase result artifact is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        )
    expected = _derive_phase_results(store, spec, ExperimentPhase(phase), experiment_refs)
    if not expected.ok:
        return expected
    if canonical_digest(expected.value) != canonical_digest(results):
        return _controller_error("Phase result claims do not match authoritative ExperimentBundles")
    task_side_runs = sum(_non_negative_int(result.get("task_side_runs", 0)) for result in expected.value.values())
    return ok(VerifiedPhaseResults(expected.value, experiment_refs, task_side_runs))


def _load_authoritative_experiment(store, spec, ref, *, candidate_id: str, phase: ExperimentPhase) -> Result:
    loaded = load_experiment_bundle(store, ref)
    if not loaded.ok:
        return loaded
    bundle = loaded.value
    expected_task_set = {
        ExperimentPhase.DISCOVERY: spec.discovery_set,
        ExperimentPhase.VALIDATION: spec.validation_set,
        ExperimentPhase.HOLDOUT: spec.holdout_set,
    }[phase]
    expected_trial_plan = freeze_trial_plan(store, expected_task_set, repetitions=spec.evaluator.repetitions)
    if not expected_trial_plan.ok:
        return expected_trial_plan
    if (
        bundle.campaign_id != spec.campaign_id
        or bundle.candidate_id != candidate_id
        or bundle.phase is not phase
        or bundle.task_set != expected_task_set
        or bundle.trial_plan != expected_trial_plan.value
        or bundle.baseline != spec.baseline
        or bundle.environment_digest != spec.environment.image_digest
        or bundle.solver_digest != spec.solver.model_digest
        or bundle.tools_digest != spec.tools_digest
        or bundle.permissions_digest != spec.permissions_digest
        or bundle.evaluator_digest != spec.evaluator.digest
        or bundle.status is not ExperimentStatus.COMPLETED
        or {metric.objective_id for metric in bundle.metrics} != {objective.id for objective in spec.objectives}
    ):
        return _controller_error("ExperimentBundle does not match the frozen campaign or phase")
    return ok(bundle)


def _experiment_score(spec: CampaignSpec, bundle) -> dict[str, Any]:
    metrics = {metric.objective_id: metric for metric in bundle.metrics}
    gates = {objective.id: objective_gate_result(objective, metrics[objective.id]) for objective in spec.objectives}
    primary = next((item for item in spec.objectives if item.id == spec.promotion_policy.primary_objective), spec.objectives[0])
    primary_metric = metrics[primary.id]
    if primary_metric.candidate_interval is None:
        primary_lcb = -1.0e308
    elif primary.direction is ObjectiveDirection.MAXIMIZE:
        primary_lcb = primary_metric.candidate_interval[0]
    else:
        primary_lcb = -primary_metric.candidate_interval[1]
    if primary_metric.delta_interval is None:
        primary_improvement_lcb = -1.0e308
    elif primary.direction is ObjectiveDirection.MAXIMIZE:
        primary_improvement_lcb = primary_metric.delta_interval[0]
    else:
        primary_improvement_lcb = -primary_metric.delta_interval[1]
    return {
        "primary_lcb": primary_lcb,
        "primary_improvement_lcb": primary_improvement_lcb,
        "critical_regressions": sum(1 for objective in spec.objectives if objective.hard and gates[objective.id].value != "passed"),
        "cost_ucb": float(bundle.usage.cost),
        "task_side_runs": bundle.usage.task_side_runs,
        "infrastructure_retry_task_side_runs": bundle.usage.infrastructure_retry_task_side_runs,
        "solver_tokens": bundle.usage.solver_tokens,
        "cost": bundle.usage.cost,
        "wall_time_seconds": bundle.usage.wall_time_seconds,
        "objective_gates": {key: value.value for key, value in gates.items()},
    }


__all__ = ["CampaignController", "FinalistSelection", "PromotionRecommendation", "SealedSearch", "publish_phase_results"]
