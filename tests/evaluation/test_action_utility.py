import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.goal_loops.contracts import STAGE_SYSTEMS, stage_protocol
from loom.evaluation.goal_loops.execution import split_unit
from loom.evaluation.goal_loops.facts import packets, run_facts
from loom.evaluation.goal_loops.review import review
from loom.evaluation.goal_loops.utility import accept_assessments, action_facts, project_utility, review_units
from loom.llm.api import LlmResponse, TokenUsage
from loom.service.semantic import GOAL_DEFAULTS
from tests.evaluation.test_goal_loops import GoalJudge, Sink, example, utility_assessment


def fixture():
    source, facts, run = example()
    view = run_facts(source, facts, run)
    rows = action_facts(source, facts, view)
    registry = {p["id"]: p for p in packets(source, view) if p.get("evidence_ref")}
    return rows, registry, view


def packet_for(registry, cid, kind="tool.completed"):
    return next(p["id"] for p in registry.values() if cid in p["call_ids"] and p["type"] == kind)


def test_same_input_and_output_are_facts_not_utility_judgments():
    rows, _, _ = fixture()
    tools = [r for r in rows if r["kind"] == "tool"]
    assert tools[0]["prior_same_input_ids"] == []
    assert tools[1]["prior_same_input_ids"] == [tools[0]["id"]]
    assert tools[1]["same_recorded_output"] is True
    projection = project_utility(rows, {})
    assert projection["coverage"] == dict(eligible=4, reviewed=0, pending=4, unknown=0)
    assert all(r["status"] == "not_reviewed" and r["value"] == "unknown" for r in projection["rows"])


def test_cited_revalidation_can_be_useful_despite_identical_output():
    rows, registry, _ = fixture()
    a, b = [r for r in rows if r["kind"] == "tool"]
    raw = utility_assessment(
        b["id"], [packet_for(registry, b["id"])], novelty="revalidation", comparison_call_ids=[a["id"]], comparison_refs=[packet_for(registry, a["id"])]
    )
    saved, refs, issues = accept_assessments([raw], registry, rows, [b["id"]], {})
    assert not issues and refs
    assert saved[b["id"]]["value"] == "limited"
    assert saved[b["id"]]["novelty"] == "revalidation"
    assert saved[b["id"]]["basis"] == "inferred"


@pytest.mark.parametrize(
    "fault", ["foreign_call", "foreign_ref", "no_own", "no_contribution", "request_only", "future_comparison", "no_comparison", "no_acceptance"]
)
def test_rejects_unsupported_claims_without_discarding_other_actions(fault):
    rows, registry, _ = fixture()
    a, b = [r for r in rows if r["kind"] == "tool"]
    first, later = packet_for(registry, a["id"]), packet_for(registry, b["id"])
    raw = utility_assessment(a["id"], [first])
    if fault == "foreign_call":
        raw["call_id"] = "another-run-call"
    if fault == "foreign_ref":
        raw["refs"] = ["undelivered"]
    if fault == "no_own":
        raw["refs"] = [later]
    if fault == "no_contribution":
        raw["value"] = "decisive"
    if fault == "request_only":
        raw.update(value="material", contribution_refs=[packet_for(registry, a["id"], "tool.started")])
    if fault == "future_comparison":
        raw.update(comparison_call_ids=[b["id"]], comparison_refs=[later])
    if fault == "no_comparison":
        raw["novelty"] = "duplicate"
    if fault == "no_acceptance":
        raw.update(role="verification", value="material", contribution_refs=[later])
    valid = utility_assessment(b["id"], [later])
    saved, _, issues = accept_assessments([raw, valid], registry, rows, [a["id"], b["id"]], {})
    assert len(issues) == 1 and set(saved) == {b["id"]}


def test_public_model_decision_can_link_to_later_observed_tool_result():
    rows, registry, _ = fixture()
    model, tool = rows[:2]
    raw = utility_assessment(
        model["id"],
        [packet_for(registry, model["id"], "llm.completed")],
        value="material",
        role="decision",
        contribution_refs=[packet_for(registry, tool["id"])],
    )
    saved, _, issues = accept_assessments([raw], registry, rows, [model["id"]], {})
    assert not issues and saved[model["id"]]["contribution_refs"]
    assert "never private reasoning" in STAGE_SYSTEMS["utility"]


def test_cross_goal_contribution_is_rejected_even_when_packet_was_delivered():
    rows, registry, _ = fixture()
    a, b = [r for r in rows if r["kind"] == "tool"]
    a["goal_end_line"] = a["end_line"]
    raw = utility_assessment(a["id"], [packet_for(registry, a["id"])], value="material", contribution_refs=[packet_for(registry, b["id"])])
    saved, _, issues = accept_assessments([raw], registry, rows, [a["id"]], {})
    assert not saved and "goal revision" in issues[0]["reason"]


def test_effective_verification_requires_and_preserves_acceptance_link():
    rows, registry, _ = fixture()
    target = next(r for r in rows if r["kind"] == "tool")
    pid = packet_for(registry, target["id"])
    registry["criterion"] = {**deepcopy(registry[pid]), "id": "criterion", "type": "verification.completed"}
    raw = utility_assessment(target["id"], [pid], role="verification", value="material", contribution_refs=[pid], acceptance_refs=["criterion"])
    saved, _, issues = accept_assessments([raw], registry, rows, [target["id"]], {})
    assert not issues and saved[target["id"]]["acceptance_refs"] == [registry["criterion"]["evidence_ref"]]


def test_window_is_bounded_and_split_keeps_action_targets():
    rows, registry, view = fixture()
    groups = review_units(rows, registry, view, 1400)
    assert [cid for group in groups for cid in group["action_ids"]] == [r["id"] for r in rows]
    assert any(g["evidence_partial"] for g in groups)
    for g in groups:
        assert sum(len(json.dumps(registry[i], ensure_ascii=False)) for i in g["packet_ids"]) <= 1400
    unit = dict(id="0", family="utility", **groups[0])
    children = split_unit(unit, registry, {})
    assert [cid for child in children for cid in child["action_ids"]] == unit["action_ids"]
    assert all(len(c["action_ids"]) < len(unit["action_ids"]) for c in children)


def test_bad_schema_row_does_not_drop_valid_sibling():
    issues = []
    valid = utility_assessment("a", ["p"])
    patch, _ = stage_protocol("utility", issues).normalize({"assessments": [{**valid, "necessity": 10}, valid]})
    assert patch["assessments"] == [valid] and len(issues) == 1


@pytest.mark.asyncio
async def test_budget_resume_reuses_saved_utilities_and_populates_execution_rows():
    source, facts, run = example()
    cp, judge = {}, GoalJudge()
    kwargs = dict(run_id=run, checkpoint=cp, save_checkpoint=lambda _: None, event_sink=Sink(), remaining_seconds=lambda: 600)
    with pytest.raises(ValueError, match="budget reached"):
        await review(source, facts, judge, settings={**GOAL_DEFAULTS, "max_calls": 2}, **kwargs)
    assert len(cp["action_utility"]) == 4
    saved = deepcopy(cp["action_utility"])
    bundle = (await review(source, facts, judge, settings=GOAL_DEFAULTS, **kwargs)).unwrap()
    assert cp["action_utility"] == saved
    assert sum(p["stage"].startswith("utility:") for p in judge.requests) == 1
    assert bundle["action_utility"]["coverage"]["pending"] == 0
    assert all(c["utility"]["status"] == "reviewed" for c in bundle["execution_view"]["calls"])
    assert bundle["cost_ledger"]["conserved"]


@pytest.mark.asyncio
async def test_omitted_assessments_are_partial_not_zero_value_or_implicit_review():
    class EmptyUtility(GoalJudge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            payload = json.loads(messages[1].content)
            if payload["stage"].startswith("utility:"):
                return ok(LlmResponse(content='{"assessments":[]}', usage=TokenUsage(20, 10, 30)))
            return await super().chat(messages, tools, cancellation, **kwargs)

    source, facts, run = example()
    result = (
        await review(
            source,
            facts,
            EmptyUtility(),
            run_id=run,
            checkpoint={},
            save_checkpoint=lambda _: None,
            event_sink=Sink(),
            settings=GOAL_DEFAULTS,
            remaining_seconds=lambda: 600,
        )
    ).unwrap()
    assert result["coverage"]["pipeline_complete"] and result["coverage"]["status"] == "partial"
    assert result["action_utility"]["coverage"]["pending"] == 4
    assert result["action_utility"]["value_counts"] == {}
