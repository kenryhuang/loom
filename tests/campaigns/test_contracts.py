from __future__ import annotations

import math
import re
import uuid
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from loom.campaigns.contracts import (
    ArtifactRef,
    CampaignBudget,
    CandidateDraft,
    CandidateHypothesis,
    CandidateKind,
    CapabilityManifest,
    ExperimentPhase,
    MetricPrediction,
    ObjectiveDirection,
    ObjectiveSpec,
    PhaseBudget,
)
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, new_prefixed_id, utc_now


def test_canonical_json_has_stable_utf8_key_order_and_digest():
    value = {"b": 1, "a": [True, None, "é"]}

    encoded = canonical_json_bytes(value)

    assert encoded == b'{"a":[true,null,"\xc3\xa9"],"b":1}'
    assert canonical_digest(value) == "17c8c6f7f948ee1c9b93b1bc35f6edc29cbaf3022bff7d076d1c4831dd8a44a2"


def test_canonical_json_normalizes_dataclass_enum_decimal_and_negative_zero():
    objective = ObjectiveSpec(
        id="success",
        direction=ObjectiveDirection.MAXIMIZE,
        aggregation="mean",
        hard=True,
        absolute_limit=0.6,
        max_baseline_regression=0.0,
        min_valid_pairs=5,
        confidence_level=0.95,
        missing_policy="fail_closed",
    )

    encoded = canonical_json_bytes({"cost": Decimal("100.00"), "negative_zero": -0.0, "objective": objective})

    assert b'"cost":"100.00"' in encoded
    assert b'"direction":"maximize"' in encoded
    assert b'"negative_zero":0' in encoded


def test_canonical_json_matches_rfc_8785_number_serialization_vector():
    encoded = canonical_json_bytes([333333333.33333329, 1e30, 4.50, 2e-3, 1e-27])

    assert encoded == b"[333333333.3333333,1e+30,4.5,0.002,1e-27]"


def test_canonical_json_rejects_non_string_object_keys():
    with pytest.raises(TypeError, match="keys"):
        canonical_json_bytes({1: "ambiguous"})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value: float):
    with pytest.raises(ValueError, match="finite"):
        canonical_json_bytes({"metric": value})


def test_prefixed_ids_are_uuid7_values_and_timestamps_are_normative():
    identifier = new_prefixed_id("cand_")
    timestamp = utc_now()

    parsed = uuid.UUID(identifier.removeprefix("cand_"))
    assert parsed.version == 7
    assert re.fullmatch(r"cand_[0-9a-f-]{36}", identifier)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", timestamp)


def test_prefixed_id_rejects_unknown_or_malformed_prefix():
    for prefix in ("", "candidate_", "../cand_", "CAND_"):
        with pytest.raises(ValueError, match="prefix"):
            new_prefixed_id(prefix)


def test_candidate_draft_recursively_freezes_nested_values():
    declaration = {"limits": {"memory_mb": 64}, "imports": ["json"]}
    draft = CandidateDraft(
        kind=CandidateKind.EXECUTABLE_COMPONENT,
        parent_ids=["cand_parent"],
        inspiration_ids=["cand_inspiration"],
        hypothesis=CandidateHypothesis(
            problem="Context selection includes irrelevant files.",
            mechanism="Rank files by task-local symbol overlap.",
            expected_improvements=[MetricPrediction("task_success", 0.05)],
            expected_regressions=[MetricPrediction("total_tokens", 100.0)],
            preserved_behaviors=["workspace confinement"],
        ),
        evidence_refs=["ev_1"],
        changed_surfaces=["context_policy"],
        artifact_path="src/candidate.py",
        patch_path=None,
        capability_manifest=CapabilityManifest(
            imports=["json"],
            dependencies=[],
            filesystem_read_roots=["/candidate", "/workspace"],
            filesystem_write_roots=["/scratch"],
            callable_tools=[],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            resource_limits=declaration,
        ),
    )

    declaration["limits"]["memory_mb"] = 4096
    declaration["imports"].append("os")

    assert draft.parent_ids == ("cand_parent",)
    assert draft.hypothesis.expected_improvements == (MetricPrediction("task_success", 0.05),)
    assert draft.capability_manifest.resource_limits["limits"]["memory_mb"] == 64
    assert draft.capability_manifest.imports == ("json",)
    with pytest.raises(TypeError):
        draft.capability_manifest.resource_limits["new"] = True
    with pytest.raises(FrozenInstanceError):
        draft.artifact_path = "other.py"


def test_hard_objective_requires_closed_evidence_contract():
    invalid = (
        {"absolute_limit": None, "max_baseline_regression": None},
        {"absolute_limit": 0.5, "min_valid_pairs": 4},
        {"absolute_limit": 0.5, "missing_policy": "exclude"},
        {"absolute_limit": 0.5, "max_baseline_regression": -0.1},
    )

    for overrides in invalid:
        values = {
            "id": "success",
            "direction": ObjectiveDirection.MAXIMIZE,
            "aggregation": "mean",
            "hard": True,
            "absolute_limit": 0.5,
            "max_baseline_regression": 0.0,
            "min_valid_pairs": 5,
            "confidence_level": 0.95,
            "missing_policy": "fail_closed",
            **overrides,
        }
        with pytest.raises(ValueError):
            ObjectiveSpec(**values)


def test_campaign_budget_rejects_duplicate_phase_or_invalid_limits():
    discovery = PhaseBudget(ExperimentPhase.DISCOVERY, max_candidate_experiments=2, max_task_side_runs=12)

    with pytest.raises(ValueError, match="duplicate"):
        CampaignBudget(
            iterations=1,
            candidates_per_iteration=1,
            phases=(discovery, discovery),
            infrastructure_retry_task_side_runs=0,
            proposer_tokens=1,
            solver_tokens=1,
            maximum_cost=Decimal("1"),
            wall_time_seconds=1,
        )


def test_artifact_ref_requires_relative_normalized_sha256_reference():
    with pytest.raises(ValueError, match="relative"):
        ArtifactRef("v1", "candidate", "/tmp/candidate.json", "0" * 64, 1)
    with pytest.raises(ValueError, match="traversal"):
        ArtifactRef("v1", "candidate", "../candidate.json", "0" * 64, 1)
    with pytest.raises(ValueError, match="sha256"):
        ArtifactRef("v1", "candidate", "candidate.json", "bad", 1)
