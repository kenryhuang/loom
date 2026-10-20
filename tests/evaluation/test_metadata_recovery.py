"""Acceptance/termination windows must not create impossible semantic retries."""

import json
from copy import deepcopy

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.evidence_store import text_value
from loom.evaluation.goal_loops.cards import compile_cards
from loom.evaluation.goal_loops.checkpoint_repair import MISSING, complete_framework_boundaries, reconcile_checkpoint
from loom.evaluation.goal_loops.facts import run_facts
from loom.evaluation.goal_loops.graph_state import initialize
from loom.evaluation.goal_loops.graph_updates import commit_updates
from loom.evaluation.goal_loops.judge_metadata import card_metadata, terminal_metadata_records
from loom.evaluation.goal_loops.structure import structure_pending
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_incremental_graph import assessment, run


def test_omitted_acceptance_boundaries_get_unknown_utility_without_claiming_task_success():
    store = source([dict(type="acceptance.plan.proposed", state="proposed"),
                    dict(type="acceptance.plan.accepted", criteria=[dict(description="All tests pass")]), *action(1)])
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    cp = initialize(view)
    cards = compile_cards(store, facts, view)["cards"]
    unit = dict(id="b", cards=[c["id"] for c in cards], cutoff=max(c["end_line"] for c in cards))
    recovered = complete_framework_boundaries(cp, cards, unit)
    assert len(recovered) == 2
    assert len(cp["assessments"]) == 2  # Solver/tool actions cannot use this fallback.
    for row in cp["assessments"].values():
        assert row["status"] == row["contribution"] == row["necessity"] == "unknown"
        assert not row["refs"] and not row["stage_ids"]
        assert "All tests pass" not in row["delta"]
    assert not complete_framework_boundaries(cp, cards, unit)


def test_saved_goal_redefinition_replay_uses_only_delivered_metadata():
    from loom.evaluation.goal_loops.judge_metadata import VERSION
    from tests.evaluation.test_graph_updates import commit, fixture

    _, _, cp, extraction = fixture()
    card = extraction["cards"][0]
    root = cp["entities"]["goals"][0]["id"]
    row = dict(key="g", parent_id=root, objective="Find cause", conditions=["Cause known"], refs=card["refs"][:1])
    assert not commit(cp, extraction, [("goals", row)])[0]
    gid = cp["entities"]["goals"][1]["id"]
    changed = {**row, "key": gid, "objective": "Apply fix", "conditions": ["Fix exists"]}
    error = dict(record_type="goals", record_id=gid,
                 reason="Stable goal definitions cannot be rewritten; declare a changed objective separately")
    cp["input_policy"] = dict(version=VERSION)
    cp["units"] = dict(b=dict(id="b", cards=[card["id"]], cutoff=max(c["end_line"] for c in extraction["cards"]), complete=False, feedback=[error]))
    cp["conflicts"] = [dict(batch_id="b", **error)]
    cp["responses"] = [dict(batch_id="b", text=json.dumps(dict(goals=[changed])), delivered_ref_ids=[])]
    metadata = {card["id"]: dict(refs=card["refs"])}
    reconcile_checkpoint(cp, extraction["cards"], metadata=metadata)
    assert len(cp["entities"]["goals"]) == 2
    assert cp["conflicts"]  # A registered ref is not enough if it was not delivered.
    cp["responses"][0]["delivered_ref_ids"] = changed["refs"]
    reconcile_checkpoint(cp, extraction["cards"], metadata=metadata)
    assert len(cp["entities"]["goals"]) == 3
    assert not cp["conflicts"]
    assert not cp["units"]["b"]["complete"]  # A new goal cannot hide its unreviewed solver card.


def saved_failure():
    events = [{**e, "usage_role": "verification"} for e in action(1)[:2]] + action(
        2, "finish", {"answer": "PRIVATE_REPORT" * 1000}, {"answer": "PRIVATE_REPORT" * 1000})[2:]
    store = source(events)
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    extraction = compile_cards(store, facts, view)
    cp = initialize(view)
    cp["extraction"] = extraction
    verifier, finish = extraction["cards"]
    row = {**assessment(verifier), "status": "unknown", "stage_ids": [], "refs": [], "contribution": "unknown", "necessity": "unknown"}
    commit_updates(cp, [("assessments", row)], batch_id="b", cards=extraction["cards"], delivered={}, cutoff=4, target_ids={verifier["id"]})
    errors = [dict(record_type="structure", reason="Subgoal analysis pending"),
              dict(record_type="assessments", record_id=finish["id"], reason="A semantic assertion requires delivered evidence"),
              dict(record_type="attributions", record_id=finish["id"], reason="Relationship awaits an accepted action assessment"),
              dict(card_ids=[finish["id"]], reason=MISSING)]
    cp["units"] = {"b": dict(id="b", cards=[c["id"] for c in extraction["cards"]], cutoff=4, complete=False, feedback=errors, repair=1)}
    cp["conflicts"] = [dict(batch_id="b", **e) for e in errors]
    cp["cursor"] = 2
    cp["responses"] = [dict(batch_id="b", text=json.dumps(dict(assessments=[dict(
        card_id=finish["id"], contribution="handoff", delta="finish invoked after acceptance gate passed", necessity="necessary",
        purpose="terminate task", refs=[], status="reviewed")], attributions=[dict(card_id=finish["id"], refs=[], stage_ids=["unproved-stage"])])),
        finish_reason="stop")]
    return store, facts, cp, verifier, finish


@pytest.mark.asyncio
async def test_resume_reconciles_real_failure_shape_without_any_model_call():
    store, facts, cp, verifier, finish = saved_failure()
    before = deepcopy(cp["assessments"][verifier["id"]])

    class NoCalls:
        model = "no-calls-fixture"

        async def chat(self, *args, **kwargs):
            raise AssertionError("Framework-only windows need no semantic retry")

    result = await run(store, facts, "run", cp, NoCalls())
    assert result["coverage"]["pipeline_complete"]
    assert not result["coverage"]["pending_cards"]
    assert not result["coverage"]["pending_structure_units"]
    assert not cp["conflicts"]
    assert cp["usage"]["calls"] == 0
    assert cp["assessments"][verifier["id"]] == before
    row = cp["assessments"][finish["id"]]
    assert row["status"] == row["necessity"] == row["contribution"] == "unknown"
    assert not row["stage_ids"] and not row["refs"]
    assert "acceptance gate passed" not in row["delta"]
    assert cp["checkpoint_reconciliations"][0]["recovered_unknown_cards"] == [finish["id"]]
    assert all(a["rule"] == "required_root_action" and a["basis"] == "framework_rule" for a in result["action_attributions"])
    # A second resume is a no-op and preserves the reconciliation audit.
    saved = deepcopy(cp["checkpoint_reconciliations"])
    await run(store, facts, "run", cp, NoCalls())
    assert cp["checkpoint_reconciliations"] == saved


@pytest.mark.parametrize("failure", ["parse", "citation", "schema"])
def test_local_reconciliation_preserves_independent_failures(failure):
    _, _, cp, _, _ = saved_failure()
    error = dict(reason=f"Unresolved {failure}", record_type="closures", record_id="other")
    cp["units"]["b"]["feedback"].append(error)
    cp["conflicts"].append(dict(batch_id="b", **error))
    reconcile_checkpoint(cp, cp["extraction"]["cards"])
    assert not cp["units"]["b"]["complete"]
    assert cp["conflicts"] == [dict(batch_id="b", **error)]


def test_solver_verification_is_not_exempted_by_a_stage_label_or_tool_name():
    store = source(action(1, tool="run_tests"))
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    cp = initialize(view)
    cp["extraction"] = compile_cards(store, facts, view)
    unit = dict(id="b", cards=[c["id"] for c in cp["extraction"]["cards"]])
    assert structure_pending(cp, unit)  # Only the explicit harness role is exempt.


def test_uncited_judgments_lose_unproven_claims_and_relationships():
    raw = dict(card_id="c", status="reviewed", refs=[], contribution="artifact", necessity="necessary",
               purpose="Fixed the bug", delta="All checks passed", stage_ids=["s"], prior_card_ids=["x"],
               uptake_card_ids=["y"], redundancy="supported", expected="Success", expected_refs=["e"])
    audits = []
    result = terminal_metadata_records([("assessments", raw), ("attributions", dict(card_id="c", stage_ids=["s"], refs=[]))],
                                       normalizations=audits, batch_id="b")
    assert len(result) == 1
    row = result[0][1]
    assert row["status"] == row["contribution"] == row["necessity"] == row["expected"] == "unknown"
    assert not row["expected_refs"]
    assert not any(k in row for k in ("stage_ids", "prior_card_ids", "uptake_card_ids", "redundancy"))
    assert "All checks passed" not in row.values() and "Fixed the bug" not in row.values()
    assert audits[0]["rule"] == "uncited_judgment_retained_as_unknown"
    # An invalid nonempty citation is not silently accepted or converted.
    invalid = {**raw, "refs": ["invented"]}
    assert terminal_metadata_records([("assessments", invalid)])[0][1] == invalid


def test_finish_and_verifier_metadata_are_citable_without_report_or_raw_output():
    store, _, cp, verifier, finish = saved_failure()
    registry = cp["extraction"]["registry"]
    finish_meta = card_metadata(store, finish, registry)
    assert not finish["refs"]  # The original extractor intentionally omitted the report body.
    assert finish_meta["refs"]
    assert {f["text"] for f in finish_meta["fields"]} >= {"finish", "tool.started", "tool.completed"}
    assert "PRIVATE_REPORT" not in json.dumps(finish_meta)
    for f in finish_meta["fields"]:
        assert text_value(store.resolve(registry[f["ref"]]["ref"])) == f["text"]
    assert card_metadata(store, verifier, registry)["usage_role"] == "verification"


def test_verifier_results_are_selected_and_feature_selection_survives_checkpoint_serialization():
    events = action(1)
    events[1]["usage_role"] = "verification"
    events[1]["response"]["content"] = json.dumps(dict(results=[dict(criterion_id="c1", status="passed", reason="Expected file exists")],
                                                       reasoning="PRIVATE_REASONING" * 1000, report="PRIVATE_REPORT" * 1000))
    store = source(events)
    facts = build_behavior_facts(store)
    extracted = compile_cards(store, facts, run_facts(store, facts, "run"))
    registry = extracted["registry"]
    meta = card_metadata(store, extracted["cards"][0], registry)
    assert any(f["field"].endswith("criterion_id") for f in meta["fields"])
    assert any(f["field"].endswith("status") for f in meta["fields"])
    assert "PRIVATE_" not in json.dumps(meta)
    for card in extracted["cards"]:
        before = card_metadata(store, card, registry)
        after = card_metadata(store, json.loads(json.dumps(card, sort_keys=True)), registry)
        assert before == after
