"""Validator upgrades preserve evidence scope, historical identities and accounting."""

import json
from copy import deepcopy

import pytest

from loom.evaluation.goal_loops.checkpoint_repair import reconcile_checkpoint
from loom.evaluation.goal_loops.graph_replay import replay_saved_response
from loom.evaluation.goal_loops.graph_updates import VALIDATION_VERSION, commit_updates
from tests.evaluation.test_graph_updates import commit, fixture
from tests.evaluation.test_incremental_graph import assessment


def saved_case():
    _, _, cp, extraction = fixture()
    card = extraction["cards"][0]
    refs = card["refs"][:1]
    root = cp["entities"]["goals"][0]["id"]
    unknown = {**assessment(card), "stage_ids": [], "status": "unknown",
               "contribution": "unknown", "necessity": "unknown", "refs": []}
    assert not commit(cp, extraction, [
        ("stages", dict(key="s", goal_id=root, title="Verify", kind="verification", refs=refs)),
        ("assessments", unknown),
    ])[0]
    stage = next(iter(cp["stages"]))
    records = [
        ("stages", dict(key=stage, goal_id=root, title="Conclude", kind="conclusion", refs=refs)),
        ("attributions", dict(card_id=card["id"], stage_ids=[stage], refs=refs)),
        ("structure", dict(decision="single_goal", reason="The current action concludes the root task. " * 7, refs=refs)),
    ]
    old_errors = [dict(code="graph.stage_immutable", reason="Old identity restriction")]
    unit = dict(id="repair", cards=[card["id"]], cutoff=card["end_line"], complete=False, feedback=deepcopy(old_errors))
    response = dict(batch_id=unit["id"], validation_version="graph-validation.1", delivered_ref_ids=refs,
                    text="\n".join(json.dumps(dict(type=kind, **row)) for kind, row in records),
                    finish_reason="stop", validation_errors=deepcopy(old_errors))
    cp.update(units={unit["id"]: unit}, responses=[response], conflicts=[dict(batch_id=unit["id"], **old_errors[0])])
    return cp, extraction, card, stage, records, unit, response


def test_unknown_judgment_can_have_independent_evidence_backed_membership():
    cp, extraction, card, stage, _, _, _ = saved_case()
    row = dict(card_id=card["id"], stage_ids=[stage], refs=card["refs"][:1])
    assert not commit(cp, extraction, [("attributions", row)])[0]
    accepted = cp["assessments"][card["id"]]
    assert accepted["status"] == accepted["contribution"] == accepted["necessity"] == "unknown"
    assert accepted["stage_ids"] == [stage]
    snapshot = deepcopy(accepted)
    errors, _ = commit_updates(cp, [("attributions", {**row, "stage_ids": []})], batch_id="unseen",
        cards=extraction["cards"], delivered={}, cutoff=card["end_line"], target_ids={card["id"]})
    assert errors[0]["code"] == "graph.relationship_evidence_scope"
    assert cp["assessments"][card["id"]] == snapshot


def test_conflicting_stage_declaration_forks_only_current_batch_and_replays_once():
    cp, extraction, card, old_id, records, _, _ = saved_case()
    old_stage = deepcopy(cp["stages"][old_id])
    assert not commit(cp, extraction, records, "repair")[0]
    new_id = cp["assessments"][card["id"]]["stage_ids"][0]
    assert new_id != old_id
    assert cp["stages"][old_id] == old_stage
    assert cp["stages"][new_id]["kind"] == "conclusion"
    assert cp["record_aliases"]["batch"]["s"] == old_id
    snapshot = deepcopy(cp)
    assert not commit(cp, extraction, records, "repair")[0]
    assert cp["graph_revision"] == snapshot["graph_revision"]
    assert cp["assessments"] == snapshot["assessments"]
    assert cp["record_aliases"] == snapshot["record_aliases"]
    other_card = extraction["cards"][1]
    assert not commit(cp, extraction, [("assessments", assessment(other_card, stage=old_id))], "other")[0]
    assert cp["assessments"][other_card["id"]]["stage_ids"] == [old_id]


def test_saved_response_upgrade_recovers_all_three_errors_without_model_calls():
    cp, extraction, card, old_id, _, unit, response = saved_case()
    original, stages, usage = deepcopy(response), deepcopy(cp["stages"]), deepcopy(cp["usage"])
    reconcile_checkpoint(cp, extraction["cards"])
    assert unit["complete"] and not unit["feedback"] and not cp["conflicts"]
    assert cp["stages"][old_id] == stages[old_id]
    assert cp["assessments"][card["id"]]["status"] == "unknown"
    assert response["text"] == original["text"]
    assert response["validation_errors"] == original["validation_errors"]
    assert response["revalidated_with"] == VALIDATION_VERSION
    assert response["revalidations"][0]["errors"] == []
    assert cp["usage"] == usage
    assert cp["checkpoint_reconciliations"][-1]["model_calls"] == 0
    snapshot = deepcopy(cp)
    reconcile_checkpoint(cp, extraction["cards"])
    assert cp == snapshot


@pytest.mark.parametrize("delivery", ["missing", "empty", "future"])
def test_replay_cannot_use_refs_outside_original_delivery(delivery):
    cp, extraction, _, old_id, _, unit, response = saved_case()
    if delivery == "missing":
        del response["delivered_ref_ids"]
    elif delivery == "empty":
        response["delivered_ref_ids"] = []
    else:
        unit["cutoff"] = 0
    old = deepcopy(cp)
    handled = replay_saved_response(cp, unit, response, extraction["cards"])
    assert handled is (delivery != "missing")
    assert not unit["complete"]
    assert cp["stages"] == old["stages"]
    assert cp["assessments"] == old["assessments"]
    if not handled:
        reconcile_checkpoint(cp, extraction["cards"])
        assert cp == old
    if handled:
        codes = {e["code"] for e in unit["feedback"]}
        assert "graph.relationship_evidence_scope" in codes
        assert "graph.structure_evidence" in codes
        snapshot = deepcopy(cp)
        reconcile_checkpoint(cp, extraction["cards"])
        assert cp == snapshot


@pytest.mark.parametrize("saved_scope", [True, False])
def test_replay_keeps_original_record_scope_and_rejects_oversized_reason(saved_scope):
    cp, extraction, card, _, records, unit, response = saved_case()
    records[-1][1]["reason"] = "x" * 601
    if saved_scope:
        response["allowed_record_types"] = ["structure"]
    else:
        response["validation_errors"].append(dict(code="response.record_type", details=dict(allowed_types=["structure"])))
    response["text"] = "\n".join(json.dumps(dict(type=kind, **row)) for kind, row in records)
    reconcile_checkpoint(cp, extraction["cards"])
    assert not unit["complete"]
    codes = {e["code"] for e in unit["feedback"]}
    assert {"response.record_type", "schema.maxLength", "graph.structure_pending"} <= codes
    error = next(e for e in unit["feedback"] if e["code"] == "schema.maxLength")
    assert error["details"]["limit"] == 600 and error["path"] == ["reason"]
    assert not cp["assessments"][card["id"]]["stage_ids"]
