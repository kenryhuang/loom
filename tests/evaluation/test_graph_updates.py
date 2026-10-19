import asyncio
import json

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.cards import compile_cards
from loom.evaluation.goal_loops.facts import run_facts
from loom.evaluation.goal_loops.graph_projection import project
from loom.evaluation.goal_loops.graph_state import initialize
from loom.evaluation.goal_loops.graph_updates import commit_updates, parse_updates
from loom.llm import LlmResponse
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_incremental_graph import GraphJudge, assessment, run


def fixture():
    store = source([*action(1), *action(2)])
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    extracted = compile_cards(store, facts, view)
    cp = initialize(view)
    cp["extraction"] = extracted
    return store, facts, cp, extracted


def commit(cp, extraction, records, batch="batch"):
    return commit_updates(cp, records, batch_id=batch, cards=extraction["cards"], delivered=extraction["registry"],
                          cutoff=max(c["end_line"] for c in extraction["cards"]), target_ids={c["id"] for c in extraction["cards"]})


def test_bad_goal_and_attribution_do_not_discard_model_or_tool_judgments():
    _, _, cp, extraction = fixture()
    cards = extraction["cards"][:2]
    records = [("goals", {"key": "bad", "objective": "Missing parent"})]
    for card in cards:
        # Invalid legacy relationship embedded in an otherwise useful judgment.
        row = {**assessment(card, stage="not-declared"), "uptake_card_ids": ["missing-card"]}
        records.append(("assessments", row))
    errors, _ = commit(cp, extraction, records)
    assert {e["record_type"] for e in errors} == {"goals", "attributions", "comparisons"}
    assert len(cp["assessments"]) == 2
    assert all(r["status"] == "reviewed" and r["stage_ids"] == [] and r["uptake_card_ids"] == [] for r in cp["assessments"].values())
    revision = cp["graph_revision"]
    commit(cp, extraction, records)
    assert cp["graph_revision"] == revision  # Retrying rejected records cannot duplicate successful writes.


def test_boundary_judgment_is_independent_and_not_an_extra_call():
    store = source([*action(1), {"type": "run.completed", "summary": "The recorded run completed"}])
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    extraction = compile_cards(store, facts, view)
    cp = initialize(view)
    cp["extraction"] = extraction
    boundary = next(c for c in extraction["cards"] if c["kind"] == "boundary")
    row = assessment(boundary, stage="missing-stage")
    errors, _ = commit(cp, extraction, [("assessments", row)])
    assert [e["record_type"] for e in errors] == ["attributions"]
    result = project(store, facts, "run", cp, extraction)
    assert result["coverage"]["cards_by_kind"]["boundary"] == dict(total=1, reviewed=1)
    assert result["coverage"]["attributed_cards"] == 0
    assert not boundary["call_ids"]
    assert result["cost_ledger"]["conserved"]


def test_later_goal_stage_attribution_and_dependency_preserve_earlier_actions():
    store, facts, cp, extraction = fixture()
    a, b = extraction["cards"][:2]
    assert b["caller_card_id"] == a["id"]
    assert b["previous_card_id"] == a["id"]
    assert {r["kind"] for r in extraction["metadata_links"] if r["target_id"] == b["id"]} == {"sequence", "invokes"}
    base = []
    for card in (a, b):
        row = assessment(card)
        row.pop("stage_ids")
        base.append(("assessments", row))
    assert not commit(cp, extraction, base, "first")[0]
    assert not cp["stages"]
    root = cp["entities"]["goals"][0]["id"]
    records = []
    for n, card in enumerate((a, b), 1):
        records += [
            ("goals", dict(key=f"g{n}", parent_id=root, objective=f"Result {n}", conditions=[f"Result {n} established"], refs=card["refs"][:1])),
            ("stages", dict(key=f"s{n}", goal_id=f"g{n}", title=f"Investigate {n}", kind="investigation", refs=card["refs"][:1])),
            ("attributions", dict(card_id=card["id"], stage_ids=[f"s{n}"], refs=card["refs"][:1])),
        ]
    records += [("relations", dict(source_id=a["id"], target_id=b["id"], kind="enables", result="Decision directs the read",
                                    refs=a["refs"][:1] + b["refs"][:1]))]
    errors, _ = commit(cp, extraction, list(reversed(records)), "second")
    assert not errors
    result = project(store, facts, "run", cp, extraction)
    assert len(result["skeleton"]["edges"]) == 1
    assigned = [n for n in result["skeleton"]["nodes"] if n["goal_id"]]
    assert {n["goal_display_id"] for n in assigned} == {"G1", "G2"}
    assert {cid for n in assigned for cid in n["call_ids"]} == set(a["call_ids"] + b["call_ids"])
    assert result["cost_ledger"]["conserved"]
    before = cp["graph_revision"]
    assert not commit(cp, extraction, records, "second")[0]
    assert cp["graph_revision"] == before


def test_jsonl_retains_complete_records_before_bad_json_or_truncated_tail():
    _, _, cp, extraction = fixture()
    a, b = extraction["cards"][:2]
    lines = [json.dumps({"type": "assessments", **assessment(a)}), '{"type":"goals","key":',
             json.dumps({"type": "assessments", **assessment(b)})]
    records, errors = parse_updates(LlmResponse(content="\n".join(lines), finish_reason="length"))
    assert len(records) == 2 and errors
    assert errors[0]["response_line"] == 2
    assert "truncated" in errors[-1]["reason"]
    commit(cp, extraction, records)
    assert len(cp["assessments"]) == 2
    # Malformed enclosing JSON is not repaired by inserting punctuation.
    records, errors = parse_updates(LlmResponse(content='{"groups":[{"goals":[]}}', finish_reason="stop"))
    assert not records and errors


def test_later_attribution_reuses_saved_judgment_without_authorizing_undelivered_claims():
    _, _, cp, extraction = fixture()
    card = extraction["cards"][0]
    row = assessment(card)
    row.pop("stage_ids")
    assert not commit(cp, extraction, [("assessments", row)])[0]
    old_ref = card["refs"][0]
    new_ref = "newly-delivered-summary"
    delivered = {new_ref: {**extraction["registry"][old_ref], "id": new_ref}}
    root = cp["entities"]["goals"][0]["id"]
    records = [
        ("stages", dict(key="later-stage", goal_id=root, title="Inspection", kind="investigation", refs=[new_ref])),
        ("attributions", dict(card_id=card["id"], stage_ids=["later-stage"], refs=[new_ref])),
    ]
    args = dict(batch_id="later", cards=extraction["cards"], delivered=delivered,
                cutoff=max(c["end_line"] for c in extraction["cards"]), target_ids=set())
    errors, _ = commit_updates(cp, records, **args)
    assert not errors
    saved = cp["assessments"][card["id"]]
    assert saved["stage_ids"] and saved["purpose"] == row["purpose"]
    assert old_ref in saved["refs"]
    # A new relationship may not cite a historical reference absent from the
    # current request, even though the host retains it for the old judgment.
    errors, _ = commit_updates(cp, [("attributions", dict(card_id=card["id"], stage_ids=[], refs=[old_ref]))], **args)
    assert "undelivered" in errors[0]["reason"]
    assert cp["assessments"][card["id"]]["stage_ids"]
    # A third window can reuse a saved judgment whose references include an
    # earlier summary pointer absent from the original extraction registry.
    next_ref = "next-window-summary"
    args["batch_id"] = "third"
    args["delivered"] = {next_ref: {**delivered[new_ref], "id": next_ref}}
    errors, _ = commit_updates(cp, [("attributions", dict(card_id=card["id"], stage_ids=saved["stage_ids"], refs=[next_ref]))], **args)
    assert not errors


@pytest.mark.asyncio
async def test_resume_orders_units_by_source_not_serialized_dictionary_keys():
    store, facts, cp, _ = fixture()
    # Establish an interrupted queue where dictionary key order is reversed.
    judge = GraphJudge()
    with pytest.raises(ValueError, match="budget reached"):
        await run(store, facts, "run", cp, judge, max_calls=1, batch_rounds=2)
    first = next(iter(cp["units"].values()))
    cp["units"]["z-earlier"] = {**first, "id": "z-earlier", "complete": False}
    cp["units"]["a-later"] = {**first, "id": "a-later", "cutoff": 8, "complete": False}
    cp["units"] = dict(sorted(cp["units"].items()))
    await run(store, facts, "run", cp, judge, max_calls=20, batch_rounds=2)
    assert judge.requests[1]["batch_id"] == "z-earlier"


@pytest.mark.asyncio
async def test_timeout_reports_request_and_retains_committed_actions():
    class Slow(GraphJudge):
        async def chat(self, *args, **kwargs):
            if self.requests:
                await asyncio.sleep(5)
            return await super().chat(*args, **kwargs)

    store, facts, cp, _ = fixture()
    with pytest.raises(ValueError, match=r"timed out after .* at batch:.*accepted records retained"):
        await run(store, facts, "run", cp, Slow(), batch_rounds=2, max_call_seconds=.02)
    assert len(cp["assessments"]) == 2
    assert cp["usage"]["unreported_calls"] == 1
