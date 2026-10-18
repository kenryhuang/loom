"""Protocol regressions: bounded stages, durable split work, evidence and migration."""

import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.goal_loops import ANALYZER, PROMPT
from loom.evaluation.goal_loops.contracts import STAGE_SCHEMAS, stage_protocol
from loom.evaluation.goal_loops.execution import reconciliation_candidates, record_review, split_unit, upgrade_checkpoint
from loom.evaluation.goal_loops.facts import packets, run_facts
from loom.evaluation.goal_loops.review import initial_state, review
from loom.evaluation.goal_loops.state import apply_patch
from loom.llm.api import LlmResponse, TokenUsage
from loom.llm.structured import native_schema
from loom.service.semantic import GOAL_DEFAULTS
from tests.evaluation.test_goal_loops import GoalJudge, Sink, empty_patch, example


def fixture_state():
    source, facts, run = example()
    view = run_facts(source, facts, run)
    delivered = {p["id"]: p for p in packets(source, view) if p.get("evidence_ref")}
    patch = empty_patch()
    patch["attempts"] = [dict(key="a", goal_id=view["roots"][0]["id"], approach="Read evidence", outcome="", refs=list(delivered), rationale="Observed")]
    state, _, issues = apply_patch(initial_state(view), patch, delivered, view["atoms"], "s")
    assert not issues
    return view, state, delivered


def test_stage_contracts_are_disjoint_and_ignore_forbidden_operations():
    assert "diagnostics" not in STAGE_SCHEMAS["structure"]["properties"]
    assert set(STAGE_SCHEMAS["reconcile"]["properties"]) == {"decisions", "updates"}
    assert not {"goals", "phases", "updates", "merges"} & STAGE_SCHEMAS["diagnose"]["properties"].keys()
    for family, field in [("structure", "diagnostics"), ("reconcile", "phases"), ("diagnose", "updates")]:
        issues = []
        value, _ = stage_protocol(family, issues).normalize({field: [{"id": "forged"}]})
        assert field not in value and issues[0]["field"] == field
    # Native providers may return null for optional change fields; they mean no edit.
    protocol = stage_protocol("structure", [])
    assert native_schema(protocol.schema)["additionalProperties"] is False
    value, _ = protocol.normalize({"updates": [dict(id="a", kind="attempts", changes={"outcome": "Done", "approach": None}, refs=["p"], rationale="Observed")]})
    assert value["updates"][0]["changes"] == {"outcome": "Done"}


def test_updates_keep_identity_and_audit_history_and_reject_root_parent_or_bad_values():
    view, state, delivered = fixture_state()
    attempt = state["attempts"][0]
    raw = dict(id=attempt["id"], kind="attempts", changes={"outcome": "Submitted"}, refs=list(delivered), rationale="Observed")
    updated, _, issues = apply_patch(state, {"updates": [raw]}, delivered, view["atoms"], "s2")
    assert not issues and len(updated["attempts"]) == 1
    assert updated["attempts"][0]["id"] == attempt["id"]
    assert updated["attempts"][0]["amendments"][0]["before"] == {"outcome": ""}
    assert state["attempts"][0]["outcome"] == ""
    for invalid in [
        dict(raw, changes={"goal_id": "other"}),
        dict(raw, changes={"outcome": []}),
        dict(raw, refs=["forged"]),
        dict(raw, id=state["goals"][0]["id"], kind="goals", changes={"objective": "Changed"}),
    ]:
        result, refs, issues = apply_patch(updated, {"updates": [invalid]}, delivered, view["atoms"], "bad")
        assert issues and not refs and result["attempts"] == updated["attempts"]
        assert result["goals"] == updated["goals"]


def test_reconciliation_only_for_evidence_backed_duplicate_candidates():
    _, state, _ = fixture_state()
    assert not reconciliation_candidates(state)
    first = state["attempts"][0]
    second = {**deepcopy(first), "id": "another"}
    state["attempts"].append(second)
    assert len(reconciliation_candidates(state)) == 1
    second["evidence_refs"] = [{**r, "line_number": r["line_number"] + 100} for r in first["evidence_refs"]]
    assert not reconciliation_candidates(state)  # Same approach can be a later legitimate retry.


@pytest.mark.asyncio
async def test_single_attempt_skips_reconciliation_model_call():
    source, facts, run = example()
    judge, cp = GoalJudge(), {}
    await review(
        source,
        facts,
        judge,
        run_id=run,
        checkpoint=cp,
        save_checkpoint=lambda _: None,
        event_sink=Sink(),
        settings=GOAL_DEFAULTS,
        remaining_seconds=lambda: 600,
    )
    assert [r["stage"].split(":")[0] for r in judge.requests] == ["structure", "utility", "diagnose"]
    assert cp["reconciliation_complete"] and cp["reconcile_candidates"] == []
    assert all(r["response_contract"]["version"] == 2 for r in judge.requests)
    assert "source_sha256" not in json.dumps(judge.requests[0]["evidence_packets"])


class SplittingJudge:
    model = "split-fixture"

    def __init__(self, always=False):
        self.requests, self.always = [], always

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        request = json.loads(messages[1].content)
        self.requests.append(request)
        truncated = self.always or len(self.requests) == 1
        return ok(LlmResponse(content="{" if truncated else "{}", finish_reason="length" if truncated else "stop", usage=TokenUsage(20, 10, 30)))


@pytest.mark.asyncio
async def test_truncation_splits_without_same_size_repair_and_resume_keeps_finished_child():
    source, facts, run = example()
    cp, judge = {}, SplittingJudge()
    with pytest.raises(ValueError, match="budget reached"):
        await review(
            source,
            facts,
            judge,
            run_id=run,
            checkpoint=cp,
            save_checkpoint=lambda _: None,
            event_sink=Sink(),
            settings={**GOAL_DEFAULTS, "max_calls": 2},
            remaining_seconds=lambda: 600,
        )
    assert cp["splits"] and cp["usage"]["calls"] == 2 and cp["usage"]["total_tokens"] == 60
    assert cp["stage_work"]["structure:0"]["pending"][0]["id"] == "0.b"
    assert judge.requests[1]["work_unit"] == "0.a" and "repair" not in judge.requests[1]
    assert len(judge.requests[1]["evidence_packets"]) < len(judge.requests[0]["evidence_packets"])
    result = await review(
        source,
        facts,
        judge,
        run_id=run,
        checkpoint=cp,
        save_checkpoint=lambda _: None,
        event_sink=Sink(),
        settings=GOAL_DEFAULTS,
        remaining_seconds=lambda: 600,
    )
    assert judge.requests[2]["work_unit"] == "0.b"
    assert result.unwrap()["coverage"]["pending_packets"] == 0
    assert sum(r["work_unit"] == "0.a" for r in judge.requests) == 1


def test_single_page_split_preserves_exact_offsets_and_requires_complete_coverage():
    source, facts, run = example()
    page = next(p for p in packets(source, run_facts(source, facts, run)) if p.get("evidence_ref"))
    page = {**page, "content": "a" * 2000, "evidence_ref": {**page["evidence_ref"], "start": 100, "end": 2100}}
    registry, derived, cp = {page["id"]: page}, {}, {"packet_status": {}}
    unit = dict(id="0", family="structure", packet_ids=[page["id"]], object_ids=[])
    children = split_unit(unit, registry, derived)
    a, b = [registry[c["packet_ids"][0]] for c in children]
    assert a["content"] + b["content"] == page["content"]
    assert a["evidence_ref"]["start"] == 100 and a["evidence_ref"]["end"] == b["evidence_ref"]["start"] == 1100
    record_review(cp, {a["id"]: a}, {a["id"]}, registry)
    assert page["id"] not in cp["packet_status"]
    record_review(cp, {b["id"]: b}, set(), registry)
    assert cp["packet_status"][page["id"]] == "classified"


@pytest.mark.asyncio
async def test_unsplittable_truncation_is_explicit_gap_and_not_retried_on_resume():
    source, facts, run = example()
    cp, judge = {}, SplittingJudge(always=True)
    result = await review(
        source,
        facts,
        judge,
        run_id=run,
        checkpoint=cp,
        save_checkpoint=lambda _: None,
        event_sink=Sink(),
        settings={**GOAL_DEFAULTS, "max_calls": 200},
        remaining_seconds=lambda: 600,
    )
    coverage = result.unwrap()["coverage"]
    assert coverage["review_gaps"] and coverage["pending_packets"] > 0
    assert coverage["pipeline_complete"] and coverage["status"] == "partial"
    assert not cp["diagnosed_goals"]
    count = len(judge.requests)
    await review(
        source,
        facts,
        judge,
        run_id=run,
        checkpoint=cp,
        save_checkpoint=lambda _: None,
        event_sink=Sink(),
        settings={**GOAL_DEFAULTS, "max_calls": 200},
        remaining_seconds=lambda: 600,
    )
    assert len(judge.requests) == count


@pytest.mark.asyncio
async def test_old_checkpoint_upgrade_retains_structure_costs_and_only_restarts_downstream():
    source, facts, run = example()
    cp, judge = {}, GoalJudge()
    with pytest.raises(ValueError, match="budget reached"):
        await review(
            source,
            facts,
            judge,
            run_id=run,
            checkpoint=cp,
            save_checkpoint=lambda _: None,
            event_sink=Sink(),
            settings={**GOAL_DEFAULTS, "max_calls": 1},
            remaining_seconds=lambda: 600,
        )
    cp["identity"].update(analyzer="goal-loops.1", prompt="goal-loops-rubric.1")
    cp["stage_work"]["reconcile:0"] = dict(next_round=2)
    cp["stages"]["reconcile:0"] = {"old": True}
    original = deepcopy(cp)
    await review(
        source,
        facts,
        judge,
        run_id=run,
        checkpoint=cp,
        save_checkpoint=lambda _: None,
        event_sink=Sink(),
        settings=GOAL_DEFAULTS,
        remaining_seconds=lambda: 600,
    )
    assert cp["identity"]["analyzer"] == ANALYZER and cp["identity"]["prompt"] == PROMPT
    assert cp["entities"]["phases"] == original["entities"]["phases"]
    assert cp["batches"] == original["batches"]
    assert cp["protocol_migrations"][0]["unfinished"]["reconcile:0"] == {"next_round": 2}
    assert sum(r["stage"].startswith("structure:") for r in judge.requests) == 1
    assert cp["usage"]["calls"] == 3
    for field in ("source", "scope", "model"):
        wrong = {**cp["identity"], field: "different"}
        with pytest.raises(ValueError, match="identity changed"):
            upgrade_checkpoint(original, wrong)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution,forged", [("merge", False), ("distinct", False), ("merge", True)])
async def test_reconcile_only_mutates_supplied_pair_with_both_endpoint_evidence(resolution, forged):
    source, facts, run = example()
    _, state, delivered = fixture_state()
    state["attempts"].append({**deepcopy(state["attempts"][0]), "id": "second"})
    cp = dict(entities=state, stages={"structure:0": {}}, batches=[list(delivered)], diagnosis_groups=[], utility_groups=[])
    first = state["attempts"][0]["id"]

    class PairJudge:
        model = "pair-fixture"

        async def chat(self, messages, tools=None, cancellation=None):
            payload = json.loads(messages[1].content)
            assert len(payload["candidates"]) == 1 and payload["stage"].startswith("reconcile:")
            data = dict(
                decisions=[
                    dict(
                        candidate_id=payload["candidates"][0]["id"],
                        resolution=resolution,
                        refs=["forged"] if forged else list(delivered),
                        rationale="Compared both endpoints",
                    )
                ],
                phases=[{"key": "forbidden"}],
            )
            return ok(LlmResponse(content=json.dumps(data), usage=TokenUsage(20, 10, 30)))

    result = (
        await review(
            source,
            facts,
            PairJudge(),
            run_id=run,
            checkpoint=cp,
            save_checkpoint=lambda _: None,
            event_sink=Sink(),
            settings=GOAL_DEFAULTS,
            remaining_seconds=lambda: 600,
        )
    ).unwrap()
    assert not result["phases"]
    assert any(d["field"] == "phases" for d in result["validation_diagnostics"])
    decision = cp["reconciliation_decisions"][0]
    assert decision["resolution"] == ("unresolved" if forged else resolution)
    assert (result["aliases"].get(first) == "second") == (resolution == "merge" and not forged)
