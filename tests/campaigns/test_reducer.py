from __future__ import annotations

import pytest

from loom.campaigns.contracts import CampaignEvent, CampaignLifecycle, CandidateLifecycle
from loom.campaigns.reducer import reduce_campaign


def _event(sequence: int, event_type: str, payload=None) -> CampaignEvent:
    return CampaignEvent(
        schema_version="loom.campaign.event.v1",
        event_id=f"evt_{sequence}",
        campaign_id="cmp_test",
        sequence=sequence,
        aggregate_version=sequence,
        event_type=event_type,
        created_at="2026-07-18T00:00:00.000000Z",
        operation_id=f"op_{sequence}",
        payload=payload or {},
        previous_hash=None if sequence == 1 else f"hash-{sequence - 1}",
        event_hash=f"hash-{sequence}",
    )


def test_reducer_projects_campaign_and_candidate_lifecycle_without_mutating_events():
    events = (
        _event(1, "campaign.created", {"spec_digest": "a" * 64}),
        _event(2, "campaign.started"),
        _event(3, "candidate.created", {"candidate_id": "cand_1"}),
        _event(4, "candidate.validation.completed", {"candidate_id": "cand_1", "valid": True}),
        _event(5, "experiment.completed", {"candidate_id": "cand_1", "artifact_ref": {"sha256": "b" * 64}}),
        _event(6, "campaign.paused"),
    )

    projection = reduce_campaign(events).unwrap()

    assert projection.lifecycle is CampaignLifecycle.PAUSED
    assert projection.aggregate_version == 6
    assert projection.candidates["cand_1"].lifecycle is CandidateLifecycle.EVALUATED
    assert events[2].payload["candidate_id"] == "cand_1"


def test_reducer_rejects_sequence_gap_and_invalid_transition():
    gap = reduce_campaign((_event(1, "campaign.created"), _event(3, "campaign.started")))
    invalid = reduce_campaign((_event(1, "campaign.created"), _event(2, "campaign.resumed")))

    assert not gap.ok and gap.error.code == "CAMPAIGN_EVENT_INVALID"
    assert not invalid.ok and invalid.error.code == "CAMPAIGN_TRANSITION_INVALID"


def test_reducer_rejects_candidate_evaluation_before_validation():
    result = reduce_campaign(
        (
            _event(1, "campaign.created"),
            _event(2, "campaign.started"),
            _event(3, "candidate.created", {"candidate_id": "cand_1"}),
            _event(4, "experiment.completed", {"candidate_id": "cand_1"}),
        )
    )

    assert not result.ok
    assert result.error.code == "CANDIDATE_TRANSITION_INVALID"


def test_projection_nested_candidate_mapping_is_read_only():
    projection = reduce_campaign(
        (
            _event(1, "campaign.created"),
            _event(2, "campaign.started"),
            _event(3, "candidate.created", {"candidate_id": "cand_1"}),
        )
    ).unwrap()

    with pytest.raises(TypeError):
        projection.candidates["cand_2"] = projection.candidates["cand_1"]
