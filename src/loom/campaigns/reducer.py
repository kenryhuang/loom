"""Deterministic campaign and candidate event projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from loom.campaigns.contracts import CampaignEvent, CampaignLifecycle, CandidateLifecycle, CandidateState
from loom.core import Result, err, make_loom_error, ok


@dataclass(frozen=True, slots=True)
class CampaignProjection:
    campaign_id: str
    aggregate_version: int
    lifecycle: CampaignLifecycle
    event_count: int
    spec_digest: str | None
    candidates: Mapping[str, CandidateState]
    imported_experience: tuple[str, ...] = ()
    latest_frontier_ref: str | None = None
    frozen_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", MappingProxyType(dict(self.candidates)))
        object.__setattr__(self, "imported_experience", tuple(self.imported_experience))

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "aggregate_version": self.aggregate_version,
            "lifecycle": self.lifecycle.value,
            "event_count": self.event_count,
            "spec_digest": self.spec_digest,
            "candidates": {
                candidate_id: {
                    "candidate_id": state.candidate_id,
                    "aggregate_version": state.aggregate_version,
                    "lifecycle": state.lifecycle.value,
                    "validation_refs": [ref.relative_path for ref in state.validation_refs],
                    "experiment_refs": [ref.relative_path for ref in state.experiment_refs],
                    "risk_ref": None if state.risk_ref is None else state.risk_ref.relative_path,
                    "latest_governance_ref": None if state.latest_governance_ref is None else state.latest_governance_ref.relative_path,
                }
                for candidate_id, state in sorted(self.candidates.items())
            },
            "imported_experience": list(self.imported_experience),
            "latest_frontier_ref": self.latest_frontier_ref,
            "frozen_reason": self.frozen_reason,
        }


def reduce_campaign(events: Iterable[CampaignEvent]) -> Result:
    ordered = tuple(events)
    if not ordered:
        return err(make_loom_error("CAMPAIGN_EVENT_INVALID", "Campaign has no events", retryable=False))
    if ordered[0].event_type != "campaign.created":
        return _event_error("First campaign event must be campaign.created")

    campaign_id = ordered[0].campaign_id
    lifecycle = CampaignLifecycle.CREATED
    candidates: dict[str, CandidateState] = {}
    imported: list[str] = []
    latest_frontier_ref = None
    frozen_reason = None

    for expected_sequence, event in enumerate(ordered, start=1):
        if event.sequence != expected_sequence or event.aggregate_version != expected_sequence:
            return _event_error("Campaign event sequence or aggregate version is not contiguous", event=event)
        if event.campaign_id != campaign_id:
            return _event_error("Campaign event belongs to another aggregate", event=event)
        event_type = event.event_type
        if expected_sequence == 1:
            continue
        if event_type in {"campaign.started", "campaign.resumed"}:
            allowed = {CampaignLifecycle.CREATED, CampaignLifecycle.PAUSED}
            if lifecycle not in allowed or (event_type == "campaign.resumed" and lifecycle is not CampaignLifecycle.PAUSED):
                return _transition_error(lifecycle, event_type)
            lifecycle = CampaignLifecycle.RUNNING
        elif event_type == "campaign.paused":
            if lifecycle not in {CampaignLifecycle.CREATED, CampaignLifecycle.RUNNING}:
                return _transition_error(lifecycle, event_type)
            lifecycle = CampaignLifecycle.PAUSED
        elif event_type == "campaign.aborted":
            if lifecycle in {CampaignLifecycle.FINALIZED, CampaignLifecycle.ABORTED}:
                return _transition_error(lifecycle, event_type)
            lifecycle = CampaignLifecycle.ABORTED
        elif event_type == "campaign.search_sealed":
            if lifecycle not in {CampaignLifecycle.RUNNING, CampaignLifecycle.PAUSED}:
                return _transition_error(lifecycle, event_type)
            lifecycle = CampaignLifecycle.SEARCH_SEALED
        elif event_type == "campaign.finalized":
            if lifecycle is not CampaignLifecycle.SEARCH_SEALED:
                return _transition_error(lifecycle, event_type)
            lifecycle = CampaignLifecycle.FINALIZED
        elif event_type == "campaign.frozen":
            lifecycle = CampaignLifecycle.FROZEN
            frozen_reason = _payload_text(event.payload, "reason")
        elif event_type == "candidate.created":
            if lifecycle is not CampaignLifecycle.RUNNING:
                return _transition_error(lifecycle, event_type)
            candidate_id = _payload_text(event.payload, "candidate_id")
            if not candidate_id or candidate_id in candidates:
                return _candidate_error("Candidate ID is missing or duplicated", candidate_id)
            candidates[candidate_id] = CandidateState(candidate_id, 1, CandidateLifecycle.PROPOSED)
        elif event_type == "candidate.validation.completed":
            if lifecycle is not CampaignLifecycle.RUNNING:
                return _transition_error(lifecycle, event_type)
            candidate_id = _payload_text(event.payload, "candidate_id")
            state = candidates.get(candidate_id)
            if state is None or state.lifecycle is not CandidateLifecycle.PROPOSED:
                return _candidate_error("Candidate validation requires proposed state", candidate_id)
            lifecycle_value = CandidateLifecycle.VALIDATED if event.payload.get("valid") is True else CandidateLifecycle.INVALID
            candidates[candidate_id] = replace(state, aggregate_version=state.aggregate_version + 1, lifecycle=lifecycle_value)
        elif event_type == "experiment.completed":
            if lifecycle is not CampaignLifecycle.RUNNING:
                return _transition_error(lifecycle, event_type)
            candidate_id = _payload_text(event.payload, "candidate_id")
            state = candidates.get(candidate_id)
            if state is None or state.lifecycle not in {CandidateLifecycle.VALIDATED, CandidateLifecycle.EVALUATED}:
                return _candidate_error("Experiment completion requires validated candidate", candidate_id)
            experiment_ref = _payload_artifact(event.payload, "experiment_ref")
            experiment_refs = state.experiment_refs if experiment_ref is None else (*state.experiment_refs, experiment_ref)
            candidates[candidate_id] = replace(
                state,
                aggregate_version=state.aggregate_version + 1,
                lifecycle=CandidateLifecycle.EVALUATED,
                experiment_refs=experiment_refs,
            )
        elif event_type.startswith("candidate."):
            transition = _candidate_lifecycle_for_event(event_type)
            if transition is not None:
                if event_type.startswith(("candidate.validation.", "candidate.holdout.")) and lifecycle is not CampaignLifecycle.SEARCH_SEALED:
                    return _transition_error(lifecycle, event_type)
                governance_events = {
                    "candidate.awaiting_approval",
                    "candidate.promoted",
                    "candidate.expired",
                    "candidate.rolled_back",
                }
                if event_type in governance_events and lifecycle is not CampaignLifecycle.FINALIZED:
                    return _transition_error(lifecycle, event_type)
                candidate_id = _payload_text(event.payload, "candidate_id")
                state = candidates.get(candidate_id)
                if state is None:
                    return _candidate_error("Candidate transition references unknown candidate", candidate_id)
                allowed = _candidate_transition_sources(event_type)
                if state.lifecycle not in allowed:
                    return _candidate_error("Candidate lifecycle transition is invalid", candidate_id)
                validation_refs = state.validation_refs
                latest_governance_ref = state.latest_governance_ref
                evidence_ref = _payload_artifact(event.payload, "evidence_ref")
                governance_ref = _payload_artifact(event.payload, "governance_ref")
                if event_type.startswith(("candidate.validation.", "candidate.holdout.")) and evidence_ref is not None:
                    validation_refs = (*validation_refs, evidence_ref)
                if governance_ref is not None:
                    latest_governance_ref = governance_ref
                candidates[candidate_id] = replace(
                    state,
                    aggregate_version=state.aggregate_version + 1,
                    lifecycle=transition,
                    validation_refs=validation_refs,
                    latest_governance_ref=latest_governance_ref,
                )
        elif event_type == "experience.imported":
            ref = event.payload.get("artifact_ref")
            if isinstance(ref, Mapping) and isinstance(ref.get("sha256"), str):
                imported.append(ref["sha256"])
        elif event_type == "frontier.updated":
            latest_frontier_ref = _payload_text(event.payload, "artifact_digest")
        elif event_type.startswith("campaign.iteration_") and lifecycle is not CampaignLifecycle.RUNNING:
            return _transition_error(lifecycle, event_type)

    return ok(
        CampaignProjection(
            campaign_id=campaign_id,
            aggregate_version=len(ordered),
            lifecycle=lifecycle,
            event_count=len(ordered),
            spec_digest=_payload_text(ordered[0].payload, "spec_digest") or None,
            candidates=candidates,
            imported_experience=tuple(imported),
            latest_frontier_ref=latest_frontier_ref,
            frozen_reason=frozen_reason,
        )
    )


def _candidate_lifecycle_for_event(event_type: str) -> CandidateLifecycle | None:
    values = {
        "candidate.validation.rejected": CandidateLifecycle.VALIDATION_REJECTED,
        "candidate.validation.passed": CandidateLifecycle.VALIDATION_PASSED,
        "candidate.holdout.rejected": CandidateLifecycle.HOLDOUT_REJECTED,
        "candidate.holdout.passed": CandidateLifecycle.HOLDOUT_PASSED,
        "candidate.awaiting_approval": CandidateLifecycle.AWAITING_APPROVAL,
        "candidate.promoted": CandidateLifecycle.PROMOTED,
        "candidate.expired": CandidateLifecycle.EXPIRED,
        "candidate.rolled_back": CandidateLifecycle.ROLLED_BACK,
    }
    return values.get(event_type)


def _candidate_transition_sources(event_type: str) -> frozenset[CandidateLifecycle]:
    values = {
        "candidate.validation.rejected": frozenset({CandidateLifecycle.VALIDATED, CandidateLifecycle.EVALUATED}),
        "candidate.validation.passed": frozenset({CandidateLifecycle.VALIDATED, CandidateLifecycle.EVALUATED}),
        "candidate.holdout.rejected": frozenset({CandidateLifecycle.VALIDATION_PASSED}),
        "candidate.holdout.passed": frozenset({CandidateLifecycle.VALIDATION_PASSED}),
        "candidate.awaiting_approval": frozenset({CandidateLifecycle.HOLDOUT_PASSED}),
        "candidate.promoted": frozenset({CandidateLifecycle.HOLDOUT_PASSED, CandidateLifecycle.AWAITING_APPROVAL}),
        "candidate.expired": frozenset({CandidateLifecycle.PROMOTED}),
        "candidate.rolled_back": frozenset({CandidateLifecycle.PROMOTED}),
    }
    return values.get(event_type, frozenset())


def _payload_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    return value if isinstance(value, str) else ""


def _payload_artifact(payload: Mapping[str, Any], field: str):
    value = payload.get(field)
    if not isinstance(value, Mapping):
        return None
    try:
        from loom.campaigns.contracts import ArtifactRef

        return ArtifactRef(**value)
    except (TypeError, ValueError):
        return None


def _event_error(message: str, *, event: CampaignEvent | None = None) -> Result:
    metadata = {} if event is None else {"sequence": event.sequence, "event_type": event.event_type}
    return err(make_loom_error("CAMPAIGN_EVENT_INVALID", message, retryable=False, metadata=metadata))


def _transition_error(lifecycle: CampaignLifecycle, event_type: str) -> Result:
    return err(
        make_loom_error(
            "CAMPAIGN_TRANSITION_INVALID",
            "Campaign lifecycle transition is invalid",
            retryable=False,
            metadata={"lifecycle": lifecycle.value, "event_type": event_type},
        )
    )


def _candidate_error(message: str, candidate_id: str) -> Result:
    return err(make_loom_error("CANDIDATE_TRANSITION_INVALID", message, retryable=False, metadata={"candidate_id": candidate_id}))


__all__ = ["CampaignProjection", "reduce_campaign"]
