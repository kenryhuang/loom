from copy import deepcopy

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.attribution import action_attributions
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_incremental_graph import GraphJudge, run


def fixture():
    cp = dict(entities=dict(goals=[
        dict(id="root", origin_line=0, end_line=100, parent_goal_id=None),
        dict(id="g1", origin_line=1, end_line=100, parent_goal_id="root"),
        dict(id="g2", origin_line=6, end_line=100, parent_goal_id="root"),
    ]), stages={"s1": dict(id="s1", goal_id="g1"), "s2": dict(id="s2", goal_id="g2")}, assessments={})
    cards = [dict(id=f"c{i}", kind="model" if i % 2 else "tool", name="inspect", start_line=i * 2 - 1, end_line=i * 2,
                  actor=dict(trace="t", loop="l", role="solver")) for i in range(1, 5)]
    cp["assessments"]["c1"] = dict(stage_ids=["s1"], evidence_refs=[dict(line_number=2)])
    return cp, cards


def test_sequential_default_chains_then_yields_to_explicit_new_goal():
    cp, cards = fixture()
    cp["assessments"]["c4"] = dict(stage_ids=["s2"], evidence_refs=[dict(line_number=8)])
    saved = deepcopy(cp)
    rows = action_attributions(cp, cards)
    assert [r["goal_id"] for r in rows] == ["g1", "g1", "g1", "g2"]
    assert [r["basis"] for r in rows] == ["semantic", "default_rule", "default_rule", "semantic"]
    assert rows[1]["anchor_card_id"] == "c1"
    assert rows[2]["anchor_card_id"] == "c2"
    assert rows[1]["evidence_refs"] == rows[2]["evidence_refs"] == []
    assert cp == saved  # Rule membership cannot change judgments or fabricate evidence.


@pytest.mark.parametrize("change", ["parallel", "overlap", "revision", "unrelated", "verification"])
def test_defaults_do_not_cross_execution_or_acceptance_boundaries(change):
    cp, cards = fixture()
    if change == "parallel":
        cards[1]["actor"]["loop"] = "another-loop"
    elif change == "overlap":
        cards[1]["start_line"] = 1
    elif change == "revision":
        cp["entities"]["goals"][0]["end_line"] = 2
        cp["entities"]["goals"].append(dict(id="new-root", origin_line=3, end_line=100, parent_goal_id=None))
    elif change == "unrelated":
        cp["assessments"]["c2"] = dict(stage_ids=[], relation="unrelated")
    else:
        cards[1]["actor"]["role"] = "verification"
    row = next(r for r in action_attributions(cp, cards) if r["card_id"] == "c2")
    assert row["goal_id"] == ("new-root" if change == "revision" else "root")
    assert row["rule"] == ("required_root_action" if change == "verification" else "current_root")
    assert row["anchor_card_id"] is None


@pytest.mark.asyncio
async def test_tree_membership_is_saved_exported_and_not_counted_as_semantic_coverage():
    import json

    from loom.core import ok
    from loom.llm import LlmResponse

    class PartialAttribution(GraphJudge):
        async def chat(self, *args, **kwargs):
            answer = (await super().chat(*args, **kwargs)).unwrap()
            value = json.loads(answer.content)
            for row in value["groups"][0]["assessments"][1:]:
                row["stage_ids"] = []
            return ok(LlmResponse(content=json.dumps(value), usage=answer.usage))

    store = source([*action(1), *action(2)])
    cp = {}
    result = await run(store, build_behavior_facts(store), "run", cp, PartialAttribution())
    assert result["coverage"]["pipeline_complete"]
    assert cp["action_attributions"] == result["action_attributions"]
    assert result["coverage"]["attributed_cards"] == 1
    assert result["coverage"]["default_attributed_cards"] == 3
    assert len({r["goal_id"] for r in result["action_attributions"]}) == 1
    assert "default_rule / previous_action_same_scope" in result["report"]["text"]
    assert result["cost_ledger"]["conserved"]


@pytest.mark.parametrize("kind,name,role", [("tool", "finish", "solver"), ("boundary", "acceptance.plan.proposed", "solver"),
                                            ("boundary", "acceptance.gate.passed", "solver"), ("model", "model", "verification")])
def test_required_actions_have_explicit_root_ownership_even_with_a_saved_subgoal(kind, name, role):
    cp, cards = fixture()
    cards[1].update(kind=kind, name=name)
    cards[1]["actor"]["role"] = role
    cp["assessments"]["c2"] = dict(stage_ids=["s2"], evidence_refs=[dict(line_number=4)])
    saved = deepcopy(cp)
    rows = {r["card_id"]: r for r in action_attributions(cp, cards)}
    assert rows["c2"]["goal_id"] == "root"
    assert rows["c2"]["basis"] == "framework_rule"
    assert rows["c2"]["rule"] == "required_root_action"
    assert rows["c2"]["stage_id"] is None
    assert rows["c3"]["goal_id"] == "g1"  # Root actions do not overwrite the solver frontier.
    assert cp == saved


@pytest.mark.asyncio
async def test_framework_projection_and_costs_override_subgoal_without_mutating_saved_review():
    from loom.evaluation.goal_loops.graph_projection import project

    store = source(action(1, tool="finish"))
    facts = build_behavior_facts(store)
    cp = {}
    await run(store, facts, "run", cp, GraphJudge())
    saved = deepcopy(cp)
    result = project(store, facts, "run", cp)
    finish = next(c for c in result["extraction"]["cards"] if c["name"] == "finish")
    root = next(g for g in result["goals"] if not g.get("parent_goal_id"))
    node = next(n for n in result["skeleton"]["nodes"] if finish["id"] in n["card_ids"])
    assert node["goal_id"] == root["id"] and node["kind"] == "framework"
    assert node["attribution"]["basis"] == "framework_rule"
    cost = next(a for a in result["cost_ledger"]["assignments"] if a["cost_atom_id"] in finish["call_ids"])
    activity = next(a for a in result["activities"] if a["id"] == cost["owner"])
    assert activity["goal_id"] == root["id"] and cost["allocation_basis"] == "framework_rule"
    assert result["coverage"]["framework_attributed_cards"] == 1
    assert result["cost_ledger"]["conserved"]
    assert "Framework-required root actions" in result["report"]["text"]
    assert cp == saved


@pytest.mark.asyncio
async def test_stage_labels_exclude_framework_and_reuse_semantic_stage_ids():
    import json

    from loom.core import ok
    from loom.llm import LlmResponse

    class Critical(GraphJudge):
        async def chat(self, *args, **kwargs):
            answer = (await super().chat(*args, **kwargs)).unwrap()
            value = json.loads(answer.content)
            for row in value["groups"][0]["assessments"]:
                row["contribution"] = "decision"  # Retain separate occurrences in the exported skeleton.
            return ok(LlmResponse(content=json.dumps(value), usage=answer.usage))

    events = action(1) + [{**e, "usage_role": "verification"} for e in action(0)[:2]] + action(2, tool="finish")
    store = source(events)
    result = await run(store, build_behavior_facts(store), "run", {}, Critical())
    nodes = result["skeleton"]["nodes"]
    framework = [n for n in nodes if n["kind"] == "framework"]
    semantic = [n for n in nodes if n["stage_id"]]
    assert len(framework) == 2 and all(n["display_id"] == "Root" for n in framework)
    assert len(semantic) > 1 and {n["display_id"] for n in semantic} == {"S1"}
    assert {s["display_id"] for s in result["stages"]} == {"S1"}
