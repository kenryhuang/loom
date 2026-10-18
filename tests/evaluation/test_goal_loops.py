import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.contracts import PATCH
from loom.evaluation.goal_loops.costs import ledger
from loom.evaluation.goal_loops.facts import packets, run_facts
from loom.evaluation.goal_loops.review import initial_state, project, review
from loom.evaluation.goal_loops.state import apply_patch
from loom.llm.api import LlmResponse, TokenUsage
from loom.service.semantic import GOAL_DEFAULTS
from tests.evaluation.test_behavior_sequence import action, source


def empty_patch():
    return {key: [] for key in PATCH["properties"]}


class GoalJudge:
    model = "goal-fixture"

    def __init__(self):
        self.requests = []

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        payload = json.loads(messages[1].content)
        self.requests.append(payload)
        result = empty_patch()
        refs = [p["id"] for p in payload["evidence_packets"] if p.get("content")]
        if payload["stage"].startswith("structure:") and refs:
            root = payload["roots"][0]["id"]
            result["attempts"] = [
                dict(
                    key="attempt",
                    goal_id=root,
                    approach="Inspect and submit",
                    outcome="Recorded only",
                    refs=refs,
                    rationale="Visible activity",
                    confidence="medium",
                )
            ]
            result["phases"] = [
                dict(
                    key="phase",
                    attempt_id="attempt",
                    kind="investigation",
                    question="What is recorded?",
                    known_before="",
                    actions="Read recorded source",
                    observations="Visible result",
                    delta="Source available",
                    remaining_questions="Completion not established",
                    next_reason="",
                    call_ids=[c["id"] for c in payload["calls"]],
                    refs=refs,
                    rationale="Recorded calls",
                    confidence="medium",
                    allocation="serves",
                )
            ]
        return ok(LlmResponse(content=json.dumps(result), usage=TokenUsage(20, 10, 30)))


class Sink:
    async def emit(self, event):
        return ok(None)


def example():
    store = source([*action(1), *action(2), {"type": "run.completed"}])
    facts = build_behavior_facts(store)
    run = next(e.run_id for e in store.events if e.run_id)
    return store, facts, run


@pytest.mark.asyncio
async def test_fixed_run_full_structure_cost_conservation_and_resume():
    store, facts, run = example()
    cp, judge = {}, GoalJudge()
    result = await review(
        store,
        facts,
        judge,
        run_id=run,
        checkpoint=cp,
        save_checkpoint=lambda _: None,
        event_sink=Sink(),
        settings={**GOAL_DEFAULTS, "max_tokens": 1000000},
        remaining_seconds=lambda: 600,
    )
    bundle = result.unwrap()
    assert bundle["schema_version"] == "loom.goal-loop-analysis.v1"
    assert bundle["coverage"]["pending_packets"] == 0
    assert bundle["coverage"]["status"] == "complete"
    assert bundle["attempts"] and bundle["phases"]
    assert bundle["cost_ledger"]["conserved"]
    assert len(bundle["cost_ledger"]["assignments"]) == len(bundle["cost_ledger"]["atoms"])
    assert bundle["source"]["digest"] == store.source_sha256
    before = len(judge.requests)
    await review(
        store, facts, judge, run_id=run, checkpoint=cp, save_checkpoint=lambda _: None, event_sink=Sink(), settings=GOAL_DEFAULTS, remaining_seconds=lambda: 600
    )
    assert len(judge.requests) == before


@pytest.mark.asyncio
async def test_budget_exhaustion_retains_structure_and_resumes_only_pending_stages():
    store, facts, run = example()
    cp, judge = {}, GoalJudge()
    with pytest.raises(ValueError, match="budget reached"):
        await review(
            store,
            facts,
            judge,
            run_id=run,
            checkpoint=cp,
            save_checkpoint=lambda _: None,
            event_sink=Sink(),
            settings={**GOAL_DEFAULTS, "max_calls": 1},
            remaining_seconds=lambda: 600,
        )
    partial = project(store, facts, run, cp)
    assert partial["phases"] and partial["coverage"]["status"] == "partial"
    await review(
        store, facts, judge, run_id=run, checkpoint=cp, save_checkpoint=lambda _: None, event_sink=Sink(), settings=GOAL_DEFAULTS, remaining_seconds=lambda: 600
    )
    assert sum(r["stage"] == "structure:0" for r in judge.requests) == 1


def test_invalid_entries_are_isolated_and_no_cross_run_or_undelivered_costs():
    store, facts, run = example()
    view = run_facts(store, facts, run)
    delivered = {p["id"]: p for p in packets(store, view) if p.get("evidence_ref")}
    refs = list(delivered)
    patch = empty_patch()
    patch["attempts"] = [
        dict(key="ok", goal_id=view["roots"][0]["id"], approach="Inspect", outcome="", refs=refs, rationale="evidence"),
        dict(key="bad", goal_id=view["roots"][0]["id"], approach="Invent", outcome="", refs=["forged"], rationale=""),
    ]
    state, _, issues = apply_patch(initial_state(view), patch, delivered, view["atoms"], "s")
    assert len(state["attempts"]) == 1
    assert len(issues) == 1
    assert len(state["validation_diagnostics"]) == 1
    assert ledger(view, state)["by_owner"]["analysis:unassigned"]["inclusive"]["model_calls"] == 2
    with pytest.raises(ValueError, match="Select a run"):
        run_facts(store, facts, "missing")


def test_one_call_shared_by_phases_has_one_attempt_owner():
    store, facts, run = example()
    view = run_facts(store, facts, run)
    state = initial_state(view)
    state["attempts"] = [dict(id="a", goal_id=state["goals"][0]["id"])]
    call = view["atoms"][0]["id"]
    state["phases"] = [dict(id="p1", attempt_id="a", call_ids=[call]), dict(id="p2", attempt_id="a", call_ids=[call])]
    costs = ledger(view, state)
    assert next(r for r in costs["assignments"] if r["cost_atom_id"] == call)["owner"] == "a"
    assert costs["by_owner"]["p1"]["inclusive"]["model_calls"] == 0
    assert costs["by_owner"]["a"]["inclusive"]["model_calls"] == 1


def test_evidence_paging_preserves_exact_ranges_and_excludes_hidden_reasoning():
    events = action(1)
    events[1]["response"]["content"] = "public " * 3000
    events[1]["response"]["reasoning_content"] = "hidden secret reasoning"
    store = source(events)
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, store.events[0].run_id)
    pages = packets(store, view, page_chars=1000)
    assert "hidden secret reasoning" not in json.dumps(pages)
    public = [p for p in pages if (p.get("evidence_ref") or {}).get("field_path") == "response.content"]
    assert "".join(p["content"] for p in public) == events[1]["response"]["content"]
    for p in public:
        assert store.resolve(p["evidence_ref"]) == p["content"]
    before = deepcopy(view)
    ledger(view, initial_state(view))
    assert before == view


def test_run_scope_keeps_original_pointers_and_costs_separate():
    events = [{**e, "run_id": "first"} for e in action(1)] + [{**e, "run_id": "second"} for e in action(1)]
    store = source(events)
    facts = build_behavior_facts(store)
    one, two = run_facts(store, facts, "first"), run_facts(store, facts, "second")
    assert {a["id"] for a in one["atoms"]}.isdisjoint(a["id"] for a in two["atoms"])
    assert min(r["line"] for r in two["records"]) == 5
    assert ledger(one, initial_state(one))["totals"]["total_tokens"]["known_sum"] == 15
    assert ledger(two, initial_state(two))["totals"]["total_tokens"]["known_sum"] == 15
    assert all(p["evidence_ref"]["source_sha256"] == store.source_sha256 for p in packets(store, two) if p["evidence_ref"])


def test_unknown_allocation_and_later_reuse_do_not_become_shared_cost():
    store, facts, run = example()
    view = run_facts(store, facts, run)
    state = initial_state(view)
    state["attempts"] = [dict(id="a", goal_id=state["goals"][0]["id"])]
    call = view["atoms"][0]["id"]
    state["phases"] = [dict(id="p1", attempt_id="a", call_ids=[call], allocation="serves"), dict(id="p2", attempt_id="a", call_ids=[call], allocation="reuse")]
    cost = ledger(view, state)
    assert cost["assignments"][0]["owner"] == "p1"
    assert cost["assignments"][0]["related_ids"] == ["p1", "p2"]
    state["phases"][1]["allocation"] = "uncertain"
    assert ledger(view, state)["assignments"][0]["owner"] == "analysis:unassigned"


def test_conflicting_usage_and_overlapping_time_are_not_zero_or_double_wall_time():
    from loom.evaluation.goal_loops.costs import summarize

    events = action(1)
    events[1]["response"]["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 100}
    store = source(events)
    view = run_facts(store, build_behavior_facts(store), "run")
    cost = ledger(view, initial_state(view))["totals"]["total_tokens"]
    assert cost == dict(known_sum=0, measured_count=0, eligible_count=1, conflict_count=1)
    atoms = [
        dict(kind="model", started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:03Z", duration_ms=3000),
        dict(kind="tool", started_at="2026-01-01T00:00:01Z", ended_at="2026-01-01T00:00:04Z", duration_ms=3000),
    ]
    values = summarize(atoms)
    assert values["wall_span_ms"] == values["observed_busy_union_ms"] == 4000
    assert values["model_duration_ms"]["known_sum"] + values["tool_duration_ms"]["known_sum"] == 6000


def test_partly_read_call_is_not_fully_reviewed():
    events = action(1)
    events[1]["response"]["content"] = "Evidence " * 2000
    store = source(events)
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    cp = {"packet_status": {packets(store, view)[0]["id"]: "reviewed_unknown"}}
    result = project(store, facts, "run", cp)
    assert result["coverage"]["reviewed_calls"] == 0
    assert result["coverage"]["pending_calls"] == 2


@pytest.mark.asyncio
async def test_schema_bad_row_does_not_erase_valid_patch():
    class BadRowJudge(GoalJudge):
        async def chat(self, *args, **kwargs):
            response = (await super().chat(*args, **kwargs)).unwrap()
            data = json.loads(response.content)
            data["goals"].append({"key": "invalid", "parent_id": 99})
            return ok(LlmResponse(content=json.dumps(data), usage=response.usage))

    store, facts, run = example()
    result = await review(
        store,
        facts,
        BadRowJudge(),
        run_id=run,
        checkpoint={},
        save_checkpoint=lambda _: None,
        event_sink=Sink(),
        settings=GOAL_DEFAULTS,
        remaining_seconds=lambda: 600,
    )
    bundle = result.unwrap()
    assert bundle["phases"]
    assert any(d["field"] == "goals" for d in bundle["validation_diagnostics"])
    assert bundle["coverage"]["status"] == "complete"


@pytest.mark.asyncio
async def test_resume_mid_stage_does_not_duplicate_saved_entities():
    class ReadingJudge(GoalJudge):
        async def chat(self, *args, **kwargs):
            response = (await super().chat(*args, **kwargs)).unwrap()
            data = json.loads(response.content)
            if len(self.requests) == 1:
                data["read_object_ids"] = [self.requests[0]["roots"][0]["id"]]
            elif self.requests[-1]["stage"] == "structure:0":
                data = empty_patch()
            return ok(LlmResponse(content=json.dumps(data), usage=response.usage))

    store, facts, run = example()
    cp, judge = {}, ReadingJudge()
    with pytest.raises(ValueError, match="budget reached"):
        await review(
            store,
            facts,
            judge,
            run_id=run,
            checkpoint=cp,
            save_checkpoint=lambda _: None,
            event_sink=Sink(),
            settings={**GOAL_DEFAULTS, "max_calls": 1},
            remaining_seconds=lambda: 600,
        )
    ids = [p["id"] for p in cp["entities"]["phases"]]
    assert ids and cp["stage_work"]["structure:0"]["pending"][0]["read_round"] == 1
    result = await review(
        store, facts, judge, run_id=run, checkpoint=cp, save_checkpoint=lambda _: None, event_sink=Sink(), settings=GOAL_DEFAULTS, remaining_seconds=lambda: 600
    )
    assert [p["id"] for p in result.unwrap()["phases"]] == ids


def test_temporal_and_exact_endpoint_evidence_checks():
    from loom.evaluation.goal_loops.state import overlaps

    a = dict(source_sha256="s", event_hash="h", line_number=1, field_path="response.content", start=0, end=10)
    assert not overlaps(a, {**a, "start": 10, "end": 20})
    assert not overlaps(a, {**a, "field_path": "response.tool_calls"})
    store, facts, run = example()
    view = run_facts(store, facts, run)
    state = initial_state(view)
    delivered = {p["id"]: p for p in packets(store, view) if p["evidence_ref"]}
    first, last = list(delivered)[0], list(delivered)[-1]
    patch = empty_patch()
    patch["attempts"] = [dict(key="a", goal_id=state["goals"][0]["id"], approach="Try", outcome="", refs=[first, last], rationale="")]
    for key, ref in [("p1", first), ("p2", last)]:
        patch["phases"].append(dict(key=key, attempt_id="a", kind="investigation", refs=[ref], call_ids=[], rationale=""))
    patch["relations"] = [dict(key="bad", source_id="p2", target_id="p1", kind="adopted_by", refs=[first, last], rationale="Future")]
    state, _, issues = apply_patch(state, patch, delivered, view["atoms"], "s")
    assert len(state["phases"]) == 2 and not state["relations"]
    assert "Future evidence" in issues[0]["reason"]


def test_reconciliation_alias_reparents_children_without_duplicating_cost():
    from loom.evaluation.goal_loops.projection import process_graph

    store, facts, run = example()
    view = run_facts(store, facts, run)
    delivered = {p["id"]: p for p in packets(store, view) if p["evidence_ref"]}
    patch = empty_patch()
    for key in ("one", "two"):
        patch["attempts"].append(
            dict(key=key, goal_id=view["roots"][0]["id"], approach="Same approach", outcome="", refs=list(delivered), rationale="Same evidence")
        )
    patch["phases"] = [dict(key="phase", attempt_id="one", kind="investigation", call_ids=[view["atoms"][0]["id"]], refs=list(delivered), rationale="Observed")]
    state, _, _ = apply_patch(initial_state(view), patch, delivered, view["atoms"], "first")
    before = ledger(view, state)["totals"]
    a, b = [r["id"] for r in state["attempts"]]
    patch = empty_patch()
    patch["merges"] = [dict(source_id=a, target_id=b, refs=list(delivered), rationale="Same ongoing attempt")]
    state, _, issues = apply_patch(state, patch, delivered, view["atoms"], "reconcile")
    assert not issues and state["aliases"][a] == b
    assert state["phases"][0]["attempt_id"] == b
    assert ledger(view, state)["totals"] == before
    graph = process_graph(state)
    ids = {n["id"] for n in graph["nodes"]}
    assert a not in ids
    assert all(e["source_id"] in ids and e["target_id"] in ids for e in graph["relations"])


def test_preexisting_index_scenario_keeps_verification_submission_and_claim_separate():
    # Scripted semantic patch: validates the contract, not real-model diagnosis accuracy.
    events = [
        *action(1, "kb_search", output={"indexed": True, "document": "guide", "revision": 1}),
        *action(2, "kb_add", output={"indexed": True, "document": "guide", "revision": 1}),
        {"type": "verification.completed", "result": {"criterion_id": "retrievable", "status": "passed", "subject": "guide@1"}},
        *action(3, "finish", output={"submitted": True}),
        {"type": "run.completed"},
    ]
    store = source(events)
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    delivered = {p["id"]: p for p in packets(store, view) if p["evidence_ref"]}
    patch = empty_patch()
    root = view["roots"][0]["id"]
    patch["attempts"] = [
        dict(key="a", goal_id=root, approach="Ensure guide is searchable", outcome="Submitted", refs=list(delivered), rationale="Visible outputs")
    ]
    for name, low, high in [("investigation", 1, 4), ("action", 5, 8), ("verification", 9, 9)]:
        refs = [pid for pid, p in delivered.items() if low <= p["line"] <= high]
        calls = [a["id"] for a in view["atoms"] if any(low <= r["line_number"] <= high for r in a["evidence_refs"])]
        patch["phases"].append(dict(key=name, attempt_id="a", kind=name, refs=refs, call_ids=calls, rationale="Recorded", allocation="serves"))
    refs = [pid for pid, p in delivered.items() if 10 <= p["line"] <= 13]
    calls = [a["id"] for a in view["atoms"] if any(10 <= r["line_number"] <= 13 for r in a["evidence_refs"])]
    patch["activities"] = [
        dict(
            key="submit",
            goal_id=root,
            kind="submission",
            description="Finish protocol",
            refs=refs,
            call_ids=calls,
            rationale="Recorded submission",
            allocation="serves",
        )
    ]
    patch["dispositions"] = [dict(goal_id=root, state="concluded", completion_claim="present", refs=refs, rationale="Submitted")]
    state, _, issues = apply_patch(initial_state(view), patch, delivered, view["atoms"], "scenario")
    assert not issues
    bundle = project(store, facts, "run", {"entities": state})
    assert [p["kind"] for p in bundle["phases"]] == ["investigation", "action", "verification"]
    assert bundle["activities"][0]["kind"] == "submission"
    assert bundle["goals"][0]["completion_claim"] == "present"
    assert bundle["facts"]["verification"][0]["value"]["result"]["subject"] == "guide@1"
    assert bundle["cost_ledger"]["totals"]["model_calls"] == 3
    assert bundle["cost_ledger"]["conserved"]
    assert not bundle["diagnostics"]  # Repetition alone does not create a waste finding.
