"""Regressions for root-only trees and the saved GLM response protocol failures."""

import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.graph_updates import parse_updates
from loom.evaluation.goal_loops.graph_contract import PROMPT, protocol, compact_contract
from loom.evaluation.goal_loops.judge_metadata import feedback_metadata
from loom.evaluation.goal_loops.structure import pending_structure_units
from loom.llm import LlmResponse, TokenUsage
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_graph_updates import commit, fixture
from tests.evaluation.test_incremental_graph import GraphJudge, assessment, run


def test_saved_response_shapes_id_declarations_root_echo_and_empty_attribution():
    _, _, cp, extraction = fixture()
    card = extraction["cards"][0]
    root = deepcopy(cp["entities"]["goals"][0])
    # The live response used id rather than key, re-declared Root with a null
    # parent, and attempted unsupported associations with refs: [].
    lines = [
        dict(type="goals", id=root["id"], parent_id=None, objective="Paraphrased root", conditions=[], refs=[]),
        dict(type="stages", id="stage:audit_speed_data", goal_id=root["id"], kind="verification",
             title="Audit planetary speed data and angular rate ratios", refs=card["refs"][:1]),
        dict(type="assessments", **assessment(card, "stage:audit_speed_data")),
        dict(type="attributions", card_id=card["id"], stage_ids=["not-established"], refs=[]),
    ]
    records, errors = parse_updates(LlmResponse(content="\n".join(json.dumps(r) for r in lines)))
    assert not errors
    assert not commit(cp, extraction, records)[0]
    assert cp["entities"]["goals"][0] == root
    saved = deepcopy(cp["assessments"][card["id"]])
    sid = saved["stage_ids"][0]
    assert cp["stages"][sid]["title"] == lines[1]["title"]
    # Existing stage echoed without delivered refs is a no-op, not a new assertion.
    assert not commit(cp, extraction, [("stages", {k: v for k, v in {**lines[1], "id": sid, "refs": []}.items() if k != "type"})])[0]
    assert cp["assessments"][card["id"]] == saved
    assert {r["rule"] for r in cp["protocol_normalizations"]} == {
        "declaration_id_to_key", "root_echo_ignored", "unsupported_attribution_omitted", "unchanged_stage_reused",
    }


def test_wire_compatibility_never_authorizes_changed_or_undelivered_assertions():
    _, _, cp, extraction = fixture()
    root = cp["entities"]["goals"][0]["id"]
    row = dict(id="stage:inspect", goal_id=root, kind="investigation", title="Inspect source", refs=["not-delivered"])
    errors, _ = commit(cp, extraction, [("stages", row)])
    assert "undelivered" in errors[0]["reason"]
    assert not cp["stages"]
    errors, _ = commit(cp, extraction, [("stages", {**row, "key": "different"})])
    assert errors[0]["reason"] == "Conflicting declaration id and key"
    row["refs"] = extraction["cards"][0]["refs"][:1]
    assert not commit(cp, extraction, [("stages", row)])[0]
    sid = next(iter(cp["stages"]))
    errors, _ = commit(cp, extraction, [("stages", {**row, "id": sid, "kind": "action", "refs": []})])
    assert errors and cp["stages"][sid]["kind"] == "investigation"


class RootOnlyJudge(GraphJudge):
    def __init__(self, repair=None):
        super().__init__()
        self.repair = repair

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        payload = json.loads(messages[1].content)
        self.requests.append(payload)
        refs = payload["cards"][0]["refs"][:1]
        root = payload["roots"][0]["id"]
        rows = {}
        if not payload["accepted_card_ids"]:
            rows = dict(stages=[dict(id="research", goal_id=root, kind="investigation", title="Inspect source", refs=refs)],
                        assessments=[assessment(c, "research") for c in payload["cards"]])
        elif self.repair == "child":
            rows = dict(
                goals=[dict(key="cause", parent_id=root, objective="Locate the source of inconsistent values",
                            conditions=["Source of inconsistency identified"], refs=refs)],
                stages=[dict(key="trace", goal_id="cause", kind="investigation", title="Trace value computation", refs=refs)],
                attributions=[dict(card_id=c["id"], stage_ids=["trace"], refs=c["refs"][:1]) for c in payload["cards"]],
            )
        elif self.repair:
            rows = dict(structure=[dict(decision=self.repair, reason="This window only retrieves one requested file", refs=refs)])
        return ok(LlmResponse(content=json.dumps(rows), usage=TokenUsage(100, 50, 150)))


@pytest.mark.asyncio
async def test_root_only_stages_do_not_pass_structure_even_with_all_actions_reviewed():
    store = source(action(1))
    cp, judge = {}, RootOnlyJudge()
    result = await run(store, build_behavior_facts(store), "run", cp, judge)
    assert result["coverage"]["pending_cards"] == 0
    assert not result["coverage"]["structure_complete"]
    assert result["coverage"]["pending_structure_units"] == pending_structure_units(cp)
    assert len(result["goals"]) == 1
    assert not cp["complete"]
    assert len(judge.requests) == 2
    assert judge.requests[-1]["review_control"]["mode"] == "structure_only"
    assert len(judge.requests[-1]["accepted_card_ids"]) == 2
    context = judge.requests[-1]["structure_review"]
    assert set(context["eligible_card_ids"]) == set(cp["assessments"])
    assert all(row["stages"] and row["stages"][0]["parent_goal_id"] is None for row in context["accepted_memberships"])
    assert "Root-stage attributions alone do not resolve it" in context["instruction"]
    assert "Existing child goals elsewhere" in context["instruction"]
    assert "Goal decomposition pending" in result["report"]["text"]


@pytest.mark.asyncio
async def test_structure_repair_adds_child_goal_without_rejudging_actions():
    store = source(action(1))
    cp, judge = {}, RootOnlyJudge("child")
    result = await run(store, build_behavior_facts(store), "run", cp, judge)
    assert result["coverage"]["pipeline_complete"]
    assert len(result["goals"]) == 2
    assert len(cp["assessments"]) == 2
    assert len(judge.requests) == 2
    child = next(g for g in result["goals"] if g.get("parent_goal_id"))
    assert child["display_id"] == "G1"
    assert all(cp["stages"][a["stage_ids"][0]]["goal_id"] == child["id"] for a in cp["assessments"].values())
    assert all(r["basis"] == "semantic" for r in result["action_attributions"])


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,complete", [("single_goal", True), ("insufficient_evidence", False)])
async def test_explicit_structure_decision_is_distinct_from_silent_missing_subgoals(decision, complete):
    store = source(action(1))
    cp, judge = {}, RootOnlyJudge(decision)
    result = await run(store, build_behavior_facts(store), "run", cp, judge)
    assert result["coverage"]["structure_complete"] is complete
    assert result["coverage"]["pipeline_complete"] is complete
    assert len(result["goals"]) == 1  # No invented subgoals to satisfy a quota.
    assert next(iter(result["structure_reviews"].values()))["decision"] == decision


@pytest.mark.asyncio
@pytest.mark.parametrize("old_prompt", ["goal-graph-rubric.5", "goal-graph-rubric.8"])
async def test_old_completed_root_only_units_resume_structure_without_losing_judgments(old_prompt):
    store = source(action(1))
    facts = build_behavior_facts(store)
    cp = {}
    await run(store, facts, "run", cp, RootOnlyJudge())
    before = {cid: a["purpose"] for cid, a in cp["assessments"].items()}
    # Simulate v5's complete flag, which only meant cards were reviewed.
    cp["identity"]["prompt"] = old_prompt
    cp["complete"] = True
    cp["conflicts"] = []
    for unit in cp["units"].values():
        unit.update(complete=True, feedback=[])
    judge = RootOnlyJudge("child")
    result = await run(store, facts, "run", cp, judge)
    assert result["coverage"]["pipeline_complete"]
    assert len(judge.requests) == 1
    assert judge.requests[0]["review_control"]["mode"] == "structure_only"
    assert {cid: a["purpose"] for cid, a in cp["assessments"].items()} == before
    assert cp["protocol_upgrades"][-1]["to_prompt"] == PROMPT


def test_structure_repair_protocol_excludes_already_accepted_action_judgments():
    expected = {"goals", "stages", "attributions", "structure"}
    assert set(protocol(False, metadata_only=True, structure_only=True).schema["properties"]) == expected
    for mode in ("text", "json_object"):
        assert set(compact_contract(mode, metadata_only=True, structure_only=True)["records"]) == expected
    assert "assessments" in protocol(False, metadata_only=True).schema["properties"]


@pytest.mark.asyncio
async def test_long_structure_reason_has_actionable_feedback_and_counts_as_validation_issue():
    class LongReasonJudge(RootOnlyJudge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            result = await super().chat(messages, tools=tools, cancellation=cancellation, **kwargs)
            rows = json.loads(result.unwrap().content)
            if rows.get("structure"):
                rows["structure"][0]["reason"] = "Root report and verification. " * 20
            return ok(LlmResponse(content=json.dumps(rows), usage=TokenUsage(100, 50, 150)))

    store = source(action(1)); facts = build_behavior_facts(store); cp = {}
    result = await run(store, facts, "run", cp, LongReasonJudge("single_goal"))
    assert result["coverage"]["pending_cards"] == 0
    assert result["coverage"]["validation_issue_count"] == 1
    assert len(result["coverage"]["pending_structure_units"]) == 1
    errors = cp["responses"][-1]["validation_errors"]
    feedback = feedback_metadata(errors)
    assert feedback[0]["path"] == ["reason"]
    assert "at most 240 characters" in feedback[0]["reason"]
    assert "Shorten the field" in feedback[0]["reason"]
    accepted = deepcopy(cp["assessments"])
    judge = RootOnlyJudge("single_goal")
    done = await run(store, facts, "run", cp, judge)
    assert done["coverage"]["pipeline_complete"]
    assert done["coverage"]["validation_issue_count"] == 0
    assert len(judge.requests) == 1
    assert cp["assessments"] == accepted
