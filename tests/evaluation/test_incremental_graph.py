import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.cards import card_payload, compile_cards
from loom.evaluation.goal_loops.facts import run_facts
from loom.evaluation.goal_loops.graph_contract import GROUP
from loom.evaluation.goal_loops.graph_projection import project
from loom.evaluation.goal_loops.graph_state import commit_group, initialize
from loom.evaluation.goal_loops.incremental import read_requests, review
from loom.llm import LlmResponse, TokenUsage
from loom.llm.api import LlmMessage, OpenAIProvider
from loom.service.semantic import GRAPH_DEFAULTS
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_goal_loops import Sink, example


def empty_group(**kwargs):
    return {**{k: [] for k in GROUP["properties"]}, **kwargs}


def assessment(card, stage="inspect", **kwargs):
    return dict(
        card_id=card["id"],
        stage_ids=[stage],
        status="reviewed",
        purpose="Inspect source",
        expected="Find relevant facts",
        expected_refs=[],
        delta="Recorded source inspection",
        contribution="evidence",
        necessity="unknown",
        redundancy="not_established",
        prior_card_ids=[],
        uptake_card_ids=[],
        refs=card["refs"][:1],
        **kwargs,
    )


class GraphJudge:
    model = "incremental-fixture"

    def __init__(self):
        self.requests = []

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        payload = json.loads(messages[1].content)
        self.requests.append(payload)
        stage = next((s["id"] for s in payload["stages"] if s["kind"] == "investigation"), "inspect")
        group = empty_group(
            assessments=[
                assessment(c, stage) if c["refs"] else {**assessment(c, stage), "status": "unknown", "stage_ids": [], "refs": []} for c in payload["cards"]
            ]
        )
        if stage == "inspect":
            refs = next(c["refs"][:1] for c in payload["cards"] if c["refs"])
            group["goals"] = [
                dict(key="understand", parent_id=payload["roots"][0]["id"], objective="Understand source behavior", conditions=["Source explained"], refs=refs)
            ]
            group["stages"] = [dict(key=stage, goal_id="understand", attempt_id="", title="Understand source behavior", kind="investigation", refs=refs)]
        return ok(LlmResponse(content=json.dumps(dict(groups=[group])), usage=TokenUsage(100, 50, 150)))


async def run(store, facts, run_id, cp, judge, **settings):
    return (
        await review(
            store,
            facts,
            judge,
            run_id=run_id,
            checkpoint=cp,
            save_checkpoint=lambda _: None,
            event_sink=Sink(),
            settings={**GRAPH_DEFAULTS, **settings},
            remaining_seconds=lambda: 600,
        )
    ).unwrap()


@pytest.mark.asyncio
async def test_incremental_card_coverage_stable_stages_costs_and_no_final_synthesis():
    store = source([e for n in range(1, 8) for e in action(n)])
    facts = build_behavior_facts(store)
    cp, judge = {}, GraphJudge()
    result = await run(store, facts, "run", cp, judge, batch_rounds=4)
    assert result["coverage"]["pipeline_complete"], cp.get("conflicts")
    assert result["coverage"]["reviewed_cards"] == 14
    assert len(result["stages"]) == 1
    assert len(result["skeleton"]["nodes"]) == 1
    assert len(result["skeleton"]["nodes"][0]["member_ids"]) == 14
    assert result["cost_ledger"]["conserved"]
    assert result["report"]["graph_revision"] == result["graph_revision"]
    assert result["skeleton"]["nodes"][0]["display_id"] == "S1"
    assert result["skeleton"]["nodes"][0]["goal_display_id"] == "G1"
    assert "G1 · Understand source behavior" in result["report"]["text"]
    assert "S1 · Understand source behavior" in result["report"]["text"]
    assert len(judge.requests) == 4
    assert all("messages" not in json.dumps(p["cards"]) for p in judge.requests)
    count = len(judge.requests)
    await run(store, facts, "run", cp, judge, batch_rounds=4)
    assert len(judge.requests) == count


@pytest.mark.asyncio
async def test_graph_wire_request_omits_output_cap_and_accepts_long_complete_response():
    judge, requests = GraphJudge(), []

    async def transport(url, request):
        body = request["body"]
        requests.append(body)
        assert "max_completion_tokens" not in body and "max_tokens" not in body
        assert body["enable_thinking"] is False
        response = (await judge.chat([LlmMessage(m["role"], m["content"]) for m in body["messages"]])).unwrap()
        return {"status": 200, "ok": True, "json": {
            "choices": [{"message": {"role": "assistant", "content": response.content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3000, "completion_tokens": 4200, "total_tokens": 7200},
        }}

    solver = OpenAIProvider(api_key="fixture", model="fixture", max_completion_tokens=2600,
                            request_options={"enable_thinking": True}, http_client=transport)
    store = source([event for n in range(1, 9) for event in action(n)])
    cp = {}
    result = await run(store, build_behavior_facts(store), "run", cp, solver, max_tokens=100)
    assert result["coverage"]["pipeline_complete"]
    assert len(requests) == 2
    assert cp["usage"]["completion_tokens"] == 8400
    assert solver.max_completion_tokens == 2600 and solver.request_options["enable_thinking"] is True


@pytest.mark.asyncio
async def test_budget_reservation_resume_and_projection_without_model():
    store, facts, rid = example()
    cp, judge = {}, GraphJudge()
    with pytest.raises(ValueError, match="budget reached"):
        await run(store, facts, rid, cp, judge, max_calls=1, batch_rounds=2)
    assert cp["graph_revision"] > 0
    partial = project(store, facts, rid, cp)
    assert partial["coverage"]["status"] == "partial"
    first = deepcopy(cp["revisions"])
    result = await run(store, facts, rid, cp, judge, max_calls=10, batch_rounds=2)
    assert result["coverage"]["pipeline_complete"], cp["conflicts"]
    assert cp["revisions"][:len(first)] == first
    assert len(judge.requests) == 3  # Two paired actions and terminal boundary.
    assert all(r["state"] == "settled" for r in cp["reservations"])


@pytest.mark.asyncio
async def test_metadata_pass_does_not_fetch_source_text_even_if_judge_requests_it():
    class ReadMore(GraphJudge):
        async def chat(self, *args, **kwargs):
            response = (await super().chat(*args, **kwargs)).unwrap()
            if len(self.requests) == 1:
                value = json.loads(response.content)
                value["evidence_requests"] = [dict(
                    question="What was the recorded intent?", purpose="Check the source statement",
                    ref=self.requests[-1]["cards"][0]["refs"][0], start=0, length=5,
                )]
                return ok(LlmResponse(content=json.dumps(value), usage=TokenUsage(100, 50, 150)))
            return ok(response)

    store = source(action(1))
    cp, judge = {"read_chars": 24000}, ReadMore()
    await run(store, build_behavior_facts(store), "run", cp, judge)
    assert cp["read_chars"] == 24000
    assert len(judge.requests) == 1
    assert not judge.requests[0]["extra_evidence"]
    assert cp["input_policy"]["source_reads"] is False


def test_atomic_group_rollback_and_explicit_unknown():
    store, facts, rid = example()
    view = run_facts(store, facts, rid)
    extracted = compile_cards(store, facts, view)
    card = extracted["cards"][0]
    cp = initialize(view)
    before = deepcopy(cp)
    group = empty_group(
        goals=[dict(key="g", parent_id=view["roots"][0]["id"], objective="Goal", conditions=[], refs=card["refs"])], assessments=[assessment(card, "missing")]
    )
    args = dict(group_id="one", base_revision=0, cards=extracted["cards"], delivered=extracted["registry"], cutoff=card["end_line"], target_ids={card["id"]})
    with pytest.raises(ValueError, match="Unknown stage"):
        commit_group(cp, group, **args)
    assert cp == before
    group = empty_group(assessments=[{**assessment(card), "stage_ids": [], "status": "unknown", "refs": []}])
    commit_group(cp, group, **args)
    assert cp["assessments"][card["id"]]["status"] == "unknown"
    assert not commit_group(cp, group, **args)  # Idempotent even with the original base revision.
    with pytest.raises(ValueError, match="Stale"):
        commit_group(cp, group, **{**args, "group_id": "other"})


def test_captured_presented_and_future_isolation():
    events = action(1, output={"content": "SECRET BODY", "shown_lines": "1-400", "next_offset": 401})
    second = action(2)
    second[0]["messages"] = [{"role": "tool", "tool_call_id": "tool1", "content": "preview; result ref"}]
    # Same trace scope is required for explicit presentation correlation.
    second[0]["trace_id"] = second[1]["trace_id"] = "trace1"
    store = source([*events, *second])
    facts = build_behavior_facts(store)
    data = compile_cards(store, facts, run_facts(store, facts, "run"))
    card = next(c for c in data["cards"] if c["kind"] == "tool" and c["start_line"] == 3)
    assert card["captured"] and card["presented"][0]["mode"] == "preview_or_transformed"
    assert not card_payload(card, 4)["presented"]
    assert "SECRET BODY" not in json.dumps(card_payload(card, 8))
    assert "401" in json.dumps(card["features"])
    ref = card["raw_refs"][-1]
    with pytest.raises(ValueError, match="future"):
        read_requests(
            store, data["registry"], [dict(ref=ref, start=0, length=10, question="What?", purpose="Verify")], cutoff=2, inspected=[], remaining_chars=100
        )
    entries = read_requests(
        store, data["registry"], [dict(ref=ref, start=0, length=10, question="What?", purpose="Verify")], cutoff=4, inspected=[], remaining_chars=100
    )
    with pytest.raises(ValueError, match="already delivered"):
        read_requests(
            store,
            data["registry"],
            [dict(ref=ref, start=0, length=10, question="Different wording", purpose="Verify")],
            cutoff=4,
            inspected=[entries[0]["ref"]],
            remaining_chars=100,
        )


@pytest.mark.asyncio
async def test_unknown_usage_remains_explicit_without_blocking_on_token_estimates():
    class Unknown(GraphJudge):
        async def chat(self, *args, **kwargs):
            answer = (await super().chat(*args, **kwargs)).unwrap()
            return ok(LlmResponse(content=answer.content, usage=TokenUsage(0, 0, 0)))

    store, facts, rid = example()
    cp, judge = {}, Unknown()
    result = await run(store, facts, rid, cp, judge, batch_rounds=2, max_tokens=100)
    assert result["coverage"]["pipeline_complete"]
    assert cp["reservations"] and all(r["state"] == "unknown" for r in cp["reservations"])
    assert cp["usage"]["unreported_calls"] == len(cp["reservations"])


@pytest.mark.asyncio
async def test_independent_groups_survive_failure_and_later_batches_continue():
    class Partial(GraphJudge):
        broken = True
        first = None

        async def chat(self, *args, **kwargs):
            response = (await super().chat(*args, **kwargs)).unwrap()
            payload = self.requests[-1]
            self.first = self.first or payload["batch_id"]
            value = json.loads(response.content)
            if payload["batch_id"] == self.first and self.broken:
                # A successful group followed by an independently invalid group.
                value["groups"].append(empty_group(assessments=[assessment(payload["cards"][0], "missing-stage")]))
            return ok(LlmResponse(content=json.dumps(value), usage=response.usage))

    store = source([*action(1), *action(2)])
    facts = build_behavior_facts(store)
    cp, judge = {}, Partial()
    partial = await run(store, facts, "run", cp, judge, batch_rounds=2)
    assert partial["coverage"]["pending_cards"] == 0
    assert partial["coverage"]["status"] == "partial"  # A rejected assertion is not hidden by coverage.
    assert len({p["batch_id"] for p in judge.requests}) == 2
    assert len(cp["stages"]) == 1
    saved = deepcopy(cp["assessments"])
    judge.broken = False
    final = await run(store, facts, "run", cp, judge, batch_rounds=2)
    assert final["coverage"]["pipeline_complete"]
    assert len(cp["stages"]) == 1
    assert final["cost_ledger"]["conserved"]
    early = judge.requests[-1]
    assert early["knowledge_cutoff"] == 4
    assert all(r["end_line"] <= 4 for r in early["frontier"])
    assert set(saved) == set(cp["assessments"])


@pytest.mark.asyncio
async def test_cross_batch_dependencies_cite_both_endpoints_and_retain_boundaries():
    class Linked(GraphJudge):
        async def chat(self, *args, **kwargs):
            response = (await super().chat(*args, **kwargs)).unwrap()
            payload = self.requests[-1]
            value = json.loads(response.content)
            if payload["frontier"]:
                group = value["groups"][0]
                target = payload["cards"][-1]
                origin = payload["frontier"][-1]
                group["relations"] = [
                    dict(
                        source_id=origin["id"],
                        target_id=group["assessments"][-1]["stage_ids"][0],
                        kind="supports",
                        result="Earlier inspection supports the follow-up",
                        refs=origin["evidence_ids"][:1] + target["refs"][:1],
                    )
                ]
            return ok(LlmResponse(content=json.dumps(value), usage=response.usage))

    store = source([*action(1), *action(2)])
    facts = build_behavior_facts(store)
    cp = {}
    result = await run(store, facts, "run", cp, Linked(), batch_rounds=2)
    assert result["coverage"]["pipeline_complete"], cp["conflicts"]
    assert len(result["skeleton"]["edges"]) == 1
    assert len(result["skeleton"]["nodes"]) > 1
    assert result["cost_ledger"]["conserved"]


@pytest.mark.asyncio
async def test_unknown_card_is_a_projection_boundary_and_expected_is_not_hindsight():
    store = source([*action(1), *action(2)])
    facts = build_behavior_facts(store)
    cp = {}
    await run(store, facts, "run", cp, GraphJudge())
    extracted = cp["extraction"]
    cards = extracted["cards"]
    cp["assessments"][cards[1]["id"]].update(stage_ids=[], status="unknown")
    projected = project(store, facts, "run", cp)
    assert len(projected["skeleton"]["nodes"]) == 3
    assert all(a["expected"] == "unknown" for a in projected["record_assessments"])
    bad = empty_group(assessments=[{**cp["assessments"][cards[0]["id"]], "expected_refs": cards[-1]["refs"][:1]}])
    with pytest.raises(ValueError, match="before the action"):
        commit_group(
            cp,
            bad,
            group_id="future-expectation",
            base_revision=cp["graph_revision"],
            cards=cards,
            delivered=extracted["registry"],
            cutoff=8,
            target_ids={c["id"] for c in cards},
        )
