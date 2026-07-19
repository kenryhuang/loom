"""Durable stage orchestration for one-command Meta-Harness campaigns."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loom.campaigns.serialization import canonical_digest
from loom.core import Result, err, make_loom_error, ok, thaw_json
from loom.optimize.contracts import OptimizationLifecycle, OptimizationResult, OptimizationSpec, OptimizationStage
from loom.optimize.control import OptimizeRunControl
from loom.optimize.events import OptimizationEventEmitter
from loom.optimize.reporting import write_report, write_result
from loom.optimize.store import SQLiteOptimizationStore


@dataclass(frozen=True, slots=True)
class OptimizeCampaignServices:
    preflight: Callable[..., Any]
    seed_evaluation: Callable[..., Any]
    seed_evolution: Callable[..., Any]
    initialize_campaign: Callable[..., Any]
    search_iteration: Callable[..., Any]
    seal_search: Callable[..., Any]
    validate: Callable[..., Any]
    select_finalists: Callable[..., Any]
    holdout: Callable[..., Any]
    finalize: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    optimization_id: str
    campaign_id: str | None
    disposition: str
    candidate_id: str | None
    recommendation_ref: Any | None
    finalist_ids: tuple[str, ...]
    holdout_opened: bool


class OptimizeOrchestrator:
    def __init__(
        self,
        spec: OptimizationSpec,
        store: SQLiteOptimizationStore,
        services: OptimizeCampaignServices,
        *,
        iterations: int,
        observer: Any | None = None,
        event_emitter: OptimizationEventEmitter | None = None,
        control: OptimizeRunControl | None = None,
        lease_seconds: int = 3600,
        governance: Callable[..., Any] | None = None,
        output_dir: str | Path | None = None,
    ):
        if iterations < 1 or lease_seconds < 1:
            raise ValueError("Optimization iterations and lease duration must be positive")
        self.spec = spec
        self.store = store
        self.services = services
        self.iterations = iterations
        self.observer = observer
        self.event_emitter = event_emitter or OptimizationEventEmitter(spec.optimization_id, spec.campaign_id, observer)
        self.control = control
        self.lease_seconds = lease_seconds
        self.governance = governance
        self.output_dir = Path(output_dir) if output_dir is not None else store.root

    async def run(self) -> Result:
        campaign = await self.run_campaign()
        if not campaign.ok:
            return campaign

        async def governance_action():
            if campaign.value.disposition == "reject":
                return ok(
                    {
                        "disposition": "rejected",
                        "candidate_id": None,
                        "promotion_decision_ref": None,
                        "monitor_ref": None,
                    }
                )
            if self.governance is None:
                return _orchestration_error("OPTIMIZATION_GOVERNANCE_MISSING", "Recommended candidate requires a governance service")
            governed = await _call(self.governance, campaign.value)
            if not governed.ok:
                return governed
            return _normalize_governance_outcome(governed.value)

        governance = await self._stage(
            OptimizationStage.GOVERNANCE_COMPLETE,
            "governance",
            {
                "campaign_id": campaign.value.campaign_id,
                "candidate_id": campaign.value.candidate_id,
                "recommendation_ref": campaign.value.recommendation_ref,
            },
            governance_action,
            terminal_lifecycle=_governance_lifecycle,
        )
        if not governance.ok:
            return governance
        disposition = str(governance.value["disposition"])
        state = await self.store.load(self.spec.optimization_id)
        if not state.ok:
            return state
        summary = {
            "schema_version": "loom.optimization.result.v1",
            "optimization_id": self.spec.optimization_id,
            "campaign_id": campaign.value.campaign_id,
            "disposition": disposition,
            "candidate_id": governance.value.get("candidate_id"),
            "promotion_decision_ref": governance.value.get("promotion_decision_ref"),
            "monitor_ref": governance.value.get("monitor_ref"),
            "task_set_digests": self.spec.task_set_digests,
            "model_digests": self.spec.model_digests,
            "finalist_ids": campaign.value.finalist_ids,
            "stages": {key.removeprefix("stage."): thaw_json(value) for key, value in state.value.outputs.items() if key.startswith("stage.")},
            "next_action": (
                f"loom optimize approve {self.spec.optimization_id} --candidate {governance.value.get('candidate_id')}"
                if disposition == "awaiting_approval"
                else "No manual action required."
            ),
        }
        report_path = write_report(self.output_dir, summary)
        result = OptimizationResult(
            "loom.optimization.result.v1",
            self.spec.optimization_id,
            campaign.value.campaign_id,
            disposition,
            governance.value.get("candidate_id"),
            governance.value.get("promotion_decision_ref"),
            governance.value.get("monitor_ref"),
            report_path,
        )
        write_result(self.output_dir, result)
        exported = await self.store.export(self.spec.optimization_id)
        return exported if not exported.ok else ok(result)

    async def run_campaign(self) -> Result:
        created = await self.store.create(self.spec)
        if not created.ok:
            return created
        if created.value.lifecycle is OptimizationLifecycle.PAUSED:
            resumed = await self.store.resume(self.spec.optimization_id)
            if not resumed.ok:
                return resumed
        reconciled = await self.store.reconcile_expired(self.spec.optimization_id)
        if not reconciled.ok:
            return reconciled

        preflight = await self._stage(
            OptimizationStage.PREFLIGHT_COMPLETE,
            "preflight",
            {"spec": self.spec.optimization_key},
            self.services.preflight,
        )
        if not preflight.ok:
            return preflight

        async def seed_action():
            evaluation = await _call(self.services.seed_evaluation)
            if not evaluation.ok:
                return evaluation
            evolution = await _call(self.services.seed_evolution, evaluation.value)
            if not evolution.ok:
                return evolution
            return ok({"evaluation": evaluation.value, "evolution": evolution.value})

        seed = await self._stage(
            OptimizationStage.SEED_ANALYSIS_COMPLETE,
            "seed_analysis",
            {"trace": str(self.spec.trace_path), "preflight": preflight.value},
            seed_action,
        )
        if not seed.ok:
            return seed
        initialized = await self._stage(
            OptimizationStage.CAMPAIGN_INITIALIZED,
            "campaign",
            {"seed": seed.value, "campaign_id": self.spec.campaign_id},
            self.services.initialize_campaign,
            seed.value,
        )
        if not initialized.ok:
            return initialized

        candidate_ids: list[str] = []
        for iteration in range(1, self.iterations + 1):
            search = await self._stage(
                OptimizationStage.SEARCH_RUNNING,
                f"search_{iteration}",
                {"iteration": iteration, "campaign": initialized.value, "prior_candidates": tuple(candidate_ids)},
                self.services.search_iteration,
                iteration,
            )
            if not search.ok:
                return search
            extracted = _ids(search.value, "candidate_ids")
            if not extracted.ok:
                return extracted
            for candidate_id in extracted.value:
                if candidate_id not in candidate_ids:
                    candidate_ids.append(candidate_id)

        sealed = await self._stage(
            OptimizationStage.SEARCH_SEALED,
            "search_sealed",
            {"candidate_ids": tuple(candidate_ids)},
            self.services.seal_search,
            tuple(candidate_ids),
        )
        if not sealed.ok:
            return sealed
        entrants = _ids(sealed.value, "entrant_ids")
        if not entrants.ok:
            return entrants
        if not set(entrants.value).issubset(candidate_ids):
            return _orchestration_error("OPTIMIZATION_ENTRANTS_INVALID", "Sealed entrants are not a subset of searched candidates")

        if entrants.value:
            validation_action = self.services.validate
            validation_args = (entrants.value,)
        else:
            validation_action = _empty_validation
            validation_args = ()
        validation = await self._stage(
            OptimizationStage.VALIDATION_COMPLETE,
            "validation",
            {"entrant_ids": entrants.value, "sealed": sealed.value},
            validation_action,
            *validation_args,
        )
        if not validation.ok:
            return validation
        validated = _ids(validation.value, "validated_ids")
        if not validated.ok:
            return validated
        if not set(validated.value).issubset(entrants.value):
            return _orchestration_error("OPTIMIZATION_VALIDATION_COHORT_INVALID", "Validated candidates are not sealed entrants")

        if validated.value:
            selection_action = self.services.select_finalists
            selection_args = (validated.value,)
        else:
            selection_action = _empty_selection
            selection_args = ()
        selection = await self._stage(
            OptimizationStage.FINALISTS_SELECTED,
            "finalists",
            {"validated_ids": validated.value, "validation": validation.value},
            selection_action,
            *selection_args,
        )
        if not selection.ok:
            return selection
        finalists = _ids(selection.value, "finalist_ids")
        if not finalists.ok:
            return finalists
        if not set(finalists.value).issubset(validated.value):
            return _orchestration_error("OPTIMIZATION_FINALISTS_INVALID", "Finalists are not validated candidates")

        async def holdout_action():
            if finalists.value:
                evaluated = await _call(self.services.holdout, finalists.value)
                if not evaluated.ok:
                    return evaluated
                evaluated_ids = _ids(evaluated.value, "evaluated_ids")
                if not evaluated_ids.ok:
                    return evaluated_ids
                if evaluated_ids.value != finalists.value:
                    return _orchestration_error(
                        "OPTIMIZATION_HOLDOUT_COHORT_MISMATCH",
                        "Holdout results do not exactly match the frozen finalist cohort",
                    )
                holdout_value = evaluated.value
            else:
                holdout_value = {"evaluated_ids": (), "experiment_refs": (), "skipped": "no_finalists"}
            finalized = await _call(self.services.finalize, finalists.value, holdout_value)
            if not finalized.ok:
                return finalized
            return ok({"holdout": holdout_value, "finalization": finalized.value})

        holdout = await self._stage(
            OptimizationStage.HOLDOUT_COMPLETE,
            "holdout",
            {"finalist_ids": finalists.value, "selection": selection.value},
            holdout_action,
        )
        if not holdout.ok:
            return holdout
        finalization = holdout.value.get("finalization")
        if not isinstance(finalization, Mapping):
            return _orchestration_error("OPTIMIZATION_FINALIZATION_INVALID", "Campaign finalization output is missing")
        disposition = finalization.get("disposition")
        candidate_id = finalization.get("candidate_id")
        if disposition not in {"recommend", "reject"} or (candidate_id is not None and not isinstance(candidate_id, str)):
            return _orchestration_error("OPTIMIZATION_FINALIZATION_INVALID", "Campaign finalization disposition is invalid")
        return ok(
            CampaignOutcome(
                self.spec.optimization_id,
                _optional_text(initialized.value.get("campaign_id")) or self.spec.campaign_id,
                str(disposition),
                candidate_id,
                finalization.get("recommendation_ref"),
                finalists.value,
                bool(finalists.value),
            )
        )

    async def _stage(
        self,
        target: OptimizationStage,
        name: str,
        inputs: Mapping[str, Any],
        action: Callable[..., Any],
        *args: Any,
        terminal_lifecycle: Callable[[Mapping[str, Any]], OptimizationLifecycle] | None = None,
    ) -> Result:
        operation_id = _operation_id(self.spec.optimization_id, name)
        if self.control is not None and self.control.requested:
            return await self._handle_control(target, operation_id)
        input_digest = canonical_digest({"optimization_key": self.spec.optimization_key, "stage": target.value, "inputs": inputs})
        begun = await self.store.begin(
            self.spec.optimization_id,
            operation_id,
            input_digest,
            target,
            lease_seconds=self.lease_seconds,
        )
        if not begun.ok:
            return begun
        output_key = f"stage.{name}"
        if begun.value.replayed:
            value = begun.value.outputs.get(output_key)
            if not isinstance(value, Mapping):
                return _orchestration_error("OPTIMIZATION_REPLAY_INVALID", "Recorded stage output is missing", operation_id=operation_id)
            await self._emit(
                "optimization.stage.replayed",
                target,
                "replayed",
                {
                    "operation_id": operation_id,
                    "lease_id": begun.value.lease_id,
                    "aggregate_version": begun.value.aggregate_version,
                    "output_keys": tuple(sorted(value)),
                },
            )
            return ok(value)
        await self._emit(
            "optimization.stage.started",
            target,
            "running",
            {
                "operation_id": operation_id,
                "lease_id": begun.value.lease_id,
                "aggregate_version": begun.value.aggregate_version,
            },
        )
        if self.control is not None and self.control.requested:
            return await self._handle_control(target, operation_id, lease_id=begun.value.lease_id)
        called = await _call(action, *args)
        if self.control is not None and self.control.requested:
            return await self._handle_control(target, operation_id, lease_id=begun.value.lease_id)
        if not called.ok:
            cancelled = await self.store.cancel(
                begun.value.lease_id,
                reason="OPTIMIZATION_STAGE_FAILED" if called.error is None else called.error.code,
            )
            if not cancelled.ok:
                return cancelled
            await self._emit(
                "optimization.stage.failed",
                target,
                "failed",
                {
                    "operation_id": operation_id,
                    "lease_id": begun.value.lease_id,
                    "error_code": "INTERNAL" if called.error is None else called.error.code,
                },
            )
            return called
        if not isinstance(called.value, Mapping):
            return _orchestration_error("OPTIMIZATION_STAGE_OUTPUT_INVALID", "Stage service must return a mapping", stage=target.value)
        lifecycle = None if terminal_lifecycle is None else terminal_lifecycle(called.value)
        completed = await self.store.complete(begun.value.lease_id, {output_key: called.value}, lifecycle=lifecycle)
        if not completed.ok:
            return completed
        await self._emit(
            "optimization.stage.completed",
            target,
            "completed",
            {
                "operation_id": operation_id,
                "lease_id": begun.value.lease_id,
                "aggregate_version": completed.value.aggregate_version,
                "output_keys": tuple(sorted(called.value)),
            },
        )
        return ok(called.value)

    async def _handle_control(
        self,
        stage: OptimizationStage,
        operation_id: str,
        *,
        lease_id: str | None = None,
    ) -> Result:
        assert self.control is not None
        request_type = self.control.request_type or "pause"
        payload = {"operation_id": operation_id}
        if lease_id is not None:
            payload["lease_id"] = lease_id
        await self._emit(f"optimization.{request_type}.requested", stage, "requested", payload)
        if lease_id is not None:
            cancelled = await self.store.cancel(lease_id, reason=f"USER_{request_type.upper()}_REQUESTED")
            if not cancelled.ok:
                return cancelled
        checkpoint = await self.control.checkpoint(self.store, self.spec.optimization_id)
        if not checkpoint.ok and checkpoint.error is not None and checkpoint.error.code == "OPTIMIZATION_PAUSED":
            await self._emit("optimization.paused", stage, "paused", payload)
        return checkpoint

    async def _emit(self, event_type: str, stage: OptimizationStage, status: str, payload: Mapping[str, Any]) -> None:
        await self.event_emitter.emit(event_type, stage=stage.value, status=status, payload=payload)


async def _call(function: Callable[..., Any], *args: Any) -> Result:
    try:
        value = function(*args)
        if inspect.isawaitable(value):
            value = await value
    except Exception as exc:
        return _orchestration_error(
            "OPTIMIZATION_SERVICE_FAILED",
            "Optimization stage service raised an exception",
            cause=exc,
        )
    if not isinstance(value, Result):
        return _orchestration_error("OPTIMIZATION_SERVICE_INVALID", "Optimization stage service must return Result")
    return value


def _ids(value: Mapping[str, Any], field: str) -> Result:
    items = value.get(field)
    if not isinstance(items, list | tuple) or not all(isinstance(item, str) and item for item in items):
        return _orchestration_error("OPTIMIZATION_COHORT_INVALID", "Optimization cohort is invalid", field=field)
    result = tuple(items)
    if len(set(result)) != len(result):
        return _orchestration_error("OPTIMIZATION_COHORT_INVALID", "Optimization cohort contains duplicates", field=field)
    return ok(result)


def _operation_id(optimization_id: str, name: str) -> str:
    return f"op_optimize_{canonical_digest({'optimization_id': optimization_id, 'name': name})[:24]}"


def _empty_validation() -> Result:
    return ok({"validated_ids": (), "experiment_refs": (), "skipped": "no_entrants"})


def _empty_selection() -> Result:
    return ok({"finalist_ids": (), "finalist_digest": canonical_digest(())})


def _normalize_governance_outcome(value: Any) -> Result:
    if isinstance(value, Mapping):
        normalized = dict(value)
    elif all(hasattr(value, field) for field in ("decision", "decision_ref", "monitor")):
        decision = value.decision
        normalized = {
            "disposition": decision.decision.value,
            "candidate_id": decision.candidate_id,
            "promotion_decision_ref": asdict(value.decision_ref),
            "monitor_ref": value.monitor,
            "risk": decision.computed_risk.value,
            "matched_risk_rules": decision.matched_risk_rules,
            "gates": tuple(asdict(gate) for gate in decision.gates),
        }
    else:
        return _orchestration_error("OPTIMIZATION_GOVERNANCE_INVALID", "Governance service returned an invalid outcome")
    disposition = normalized.get("disposition")
    if disposition not in {"promoted", "rejected", "awaiting_approval"}:
        return _orchestration_error("OPTIMIZATION_GOVERNANCE_INVALID", "Governance disposition is invalid")
    monitor = normalized.get("monitor_ref")
    if disposition == "promoted" and not isinstance(monitor, Mapping):
        return _orchestration_error("OPTIMIZATION_GOVERNANCE_INVALID", "Promoted outcome requires an active monitor reference")
    if disposition != "promoted" and monitor is not None:
        return _orchestration_error("OPTIMIZATION_GOVERNANCE_INVALID", "Non-promoted outcome cannot contain an active monitor")
    candidate_id = normalized.get("candidate_id")
    if candidate_id is not None and not isinstance(candidate_id, str):
        return _orchestration_error("OPTIMIZATION_GOVERNANCE_INVALID", "Governance candidate ID is invalid")
    return ok(normalized)


def _governance_lifecycle(value: Mapping[str, Any]) -> OptimizationLifecycle:
    return {
        "promoted": OptimizationLifecycle.PROMOTED,
        "rejected": OptimizationLifecycle.REJECTED,
        "awaiting_approval": OptimizationLifecycle.AWAITING_APPROVAL,
    }[str(value["disposition"])]


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _orchestration_error(code: str, message: str, *, cause: BaseException | None = None, **metadata: Any) -> Result:
    return err(
        make_loom_error(
            code,
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
            metadata=metadata,
        )
    )


__all__ = ["CampaignOutcome", "OptimizeCampaignServices", "OptimizeOrchestrator"]
