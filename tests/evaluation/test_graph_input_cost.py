import json

import pytest

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.cards import CARD_CHARS, card_payload, compile_cards
from loom.evaluation.goal_loops.facts import run_facts
from loom.evaluation.goal_loops.graph_contract import SYSTEM, compact_contract, protocol
from loom.evaluation.goal_loops.judge_metadata import card_metadata
from loom.evaluation.goal_loops.packing import STATE_CHARS, bounded_state, request_profile, serialized
from loom.llm.api import LlmMessage
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_incremental_graph import GraphJudge, run


def test_boundary_metadata_never_copies_opaque_results_or_hidden_reasoning():
    events = [
        *action(1),
        {
            "type": "verification.completed",
            "result": {
                "status": "passed",
                "stdout": "RAW_RESULT_BODY" * 10000,
                "checks": [{"description": "Expected file exists", "passed": True}],
                "reasoning_content": "PRIVATE_REASONING" * 10000,
            },
        },
    ]
    events[1]["response"]["reasoning_content"] = "PRIVATE_REASONING" * 10000
    events[0]["messages"] = [{"role": "user", "content": "RAW_REQUEST_HISTORY" * 10000}]
    store = source(events)
    facts = build_behavior_facts(store)
    compiled = compile_cards(store, facts, run_facts(store, facts, "run"))
    payload = serialized([card_payload(c, 100) for c in compiled["cards"]])
    assert "Expected file exists" in payload and "passed" in payload
    assert not any(marker in payload for marker in ("RAW_RESULT_BODY", "PRIVATE_REASONING", "RAW_REQUEST_HISTORY"))
    assert len(payload) < 6000


def test_root_and_retrieved_metadata_have_an_independent_size_limit():
    state = dict(
        roots=[
            dict(
                id="root",
                objective="Root " * 10000,
                conditions=[dict(id=str(n), description="Condition " * 10000, check={"raw": "UNWANTED" * 10000}) for n in range(25)],
            )
        ],
        goals=[dict(id=f"g{n}", objective="Goal " * 1000, conditions=["C" * 1000] * 10) for n in range(6)],
        stages=[],
        closures=[],
        older_goal_index=[],
        omitted_goals=400,
    )
    result = bounded_state(state)
    assert len(serialized(result)) <= STATE_CHARS
    assert result["roots"][0]["id"] == "root"
    assert result["state_limits"]["truncated_fields"] > 0
    assert result["state_limits"]["omitted_rows"]
    assert "UNWANTED" not in serialized(result)
    assert len(state["roots"][0]["objective"]) == 50000  # Local authoritative state stays intact.


def test_large_boundary_card_drops_metadata_with_explicit_omissions_and_exact_refs():
    events = [*action(1), {"type": "acceptance.plan.proposed", "criteria": [dict(description="Condition " * 100, status="pending") for _ in range(20)]}]
    store = source(events)
    facts = build_behavior_facts(store)
    data = compile_cards(store, facts, run_facts(store, facts, "run"))
    original = data["cards"][-1]
    before = serialized(original)
    payload = card_payload(original, 100)
    assert len(serialized(payload)) <= CARD_CHARS
    assert payload["omitted_metadata_fields"] > 0
    assert len(payload["refs"]) < len(original["refs"])
    assert serialized(original) == before
    assert all(ref in data["registry"] for ref in payload["refs"])


def test_presentation_history_is_metadata_and_not_a_growing_prompt():
    store = source(action(1))
    facts = build_behavior_facts(store)
    card = compile_cards(store, facts, run_facts(store, facts, "run"))["cards"][-1]
    card["presented"] = [dict(ref=f"e{n}", line=n, chars=10000, mode="preview_or_transformed") for n in range(1, 1001)]
    payload = card_payload(card, 500)
    assert payload["presentation_count"] == 500
    assert [p["line"] for p in payload["presented"]] == [498, 499, 500]
    assert len(serialized(payload)) < 3000


def test_native_schema_is_included_in_input_limit_reservation_and_breakdown():
    payload = dict(
        cards=[], roots=[], goals=[], stages=[], closures=[], older_goal_index=[], state_limits={}, frontier=[], boundary_evidence=[], extra_evidence=[]
    )
    messages = [LlmMessage("system", SYSTEM), LlmMessage("user", serialized(payload))]
    native = request_profile(messages, payload, "json_schema", protocol())
    plain = request_profile(messages, payload, "text", protocol())
    assert native["schema_chars"] > 4000
    assert native["total_chars"] == plain["total_chars"] + native["schema_chars"]
    assert native["input_estimate"] > plain["input_estimate"] + 4000
    assert sum(native["components_chars"].values()) == native["total_chars"]
    assert native["components_chars"]["envelope_and_control"] >= 0
    payload["contract"] = compact_contract()
    text = request_profile([messages[0], LlmMessage("user", serialized(payload))], payload, "text", protocol())
    assert text["schema_chars"] == 0 and text["components_chars"]["contract"] > 0


@pytest.mark.asyncio
async def test_schema_cost_is_measured_without_a_token_budget_gate():
    store = source(action(1))
    facts = build_behavior_facts(store)
    cp, judge = {"mode": "json_schema"}, GraphJudge()
    result = await run(store, facts, "run", cp, judge, max_tokens=100)
    assert result["coverage"]["pipeline_complete"]
    assert judge.requests and cp["usage"]["total_tokens"] > 100
    assert 0 < cp["reservations"][0]["input_profile"]["schema_chars"] < 4000
    assert cp["reservations"][0]["output_token_limit"] is None


def test_large_tool_output_changes_metadata_size_not_prompt_body_size():
    sizes = []
    for repetitions in (1, 100000):
        store = source(action(1, output={"content": "OPAQUE_BODY" * repetitions, "status": "success"}))
        facts = build_behavior_facts(store)
        data = compile_cards(store, facts, run_facts(store, facts, "run"))
        payload = serialized([card_payload(c, 100) for c in data["cards"]])
        assert "OPAQUE_BODY" not in payload
        sizes.append(len(payload))
    assert abs(sizes[1] - sizes[0]) < 100


@pytest.mark.asyncio
async def test_longer_trace_adds_batches_without_accumulating_prompt_history():
    measurements = []
    for count in (8, 32):
        store = source([event for n in range(1, count + 1) for event in action(n, output={"content": "RAW_TOOL_BODY" * 1000, "status": "success"})])
        facts = build_behavior_facts(store)
        cp, judge = {}, GraphJudge()
        result = await run(store, facts, "run", cp, judge, max_calls=40)
        assert result["coverage"]["pipeline_complete"], cp["conflicts"]
        assert all(len(p["cards"]) <= 8 and "RAW_TOOL_BODY" not in serialized(p) for p in judge.requests)
        assert all("responses" not in p and "messages" not in p for p in judge.requests)
        profiles = [r["input_profile"] for r in cp["reservations"]]
        assert all(p["total_chars"] <= 24000 for p in profiles)
        assert all(sum(p["components_chars"].values()) == p["total_chars"] for p in profiles)
        measurements.append((len(profiles), max(p["total_chars"] for p in profiles)))
    assert measurements[1][0] == 4 * measurements[0][0]
    assert measurements[1][1] < 1.2 * measurements[0][1]


@pytest.mark.asyncio
async def test_pending_request_bounds_old_feedback_without_replaying_saved_cards():
    store = source([event for n in range(1, 9) for event in action(n)])
    facts = build_behavior_facts(store)
    cp, judge = {"mode": "json_schema"}, GraphJudge()
    with pytest.raises(ValueError, match="budget reached"):
        await run(store, facts, "run", cp, judge, max_calls=1, max_prompt_chars=24000)
    saved = {cid: dict(row) for cid, row in cp["assessments"].items()}
    pending = next(u for u in cp["units"].values() if not u["complete"])
    # A retained repair/read window is fixed; it must not lose cards just to fit
    # an earlier limit. Simulate the metadata growth that caused the real failure.
    pending["feedback"] = [dict(reason="Saved validation feedback. " * 800)]
    saved_ref = next(iter(cp["extraction"]["registry"].values()))
    cp["extra_evidence"]["old-read"] = {**saved_ref, "id": "old-read", "text": "OLD_RAW_TOOL_BODY" * 10000, "question": "Legacy read"}
    pending["extra"] = ["old-read"]
    result = await run(store, facts, "run", cp, judge, max_calls=20, max_prompt_chars=262144)
    assert result["coverage"]["pipeline_complete"]
    assert len(judge.requests) == 2
    assert cp["reservations"][-1]["input_profile"]["total_chars"] < 24000
    assert len(serialized(judge.requests[-1]["feedback"])) < 1600
    assert all(len(e["reason"]) <= 180 and e["code"] for e in judge.requests[-1]["feedback"])
    assert "OLD_RAW_TOOL_BODY" not in serialized(judge.requests[-1])
    assert not judge.requests[-1]["extra_evidence"]
    assert {c["id"] for c in judge.requests[-1]["cards"]}.isdisjoint(saved)
    assert all(cp["assessments"][cid] == row for cid, row in saved.items())
    with pytest.raises(ValueError, match="cannot reduce resource"):
        await run(store, facts, "run", cp, judge, max_calls=20, max_prompt_chars=24000)


def test_model_action_json_is_extracted_locally_without_reasoning_patch_or_report():
    events = action(1)
    events[1]["response"]["content"] = "```json\n" + json.dumps({
        "reasoning": "RAW_REASONING" * 10000,
        "action": {"kind": "tool", "description": "Inspect orbital calculation", "target": "shell_execute",
                   "input": {"command": "rg angular_velocity src", "patch": "RAW_PATCH" * 10000}},
        "report": "RAW_REPORT" * 10000,
    }) + "\n```"
    store = source(events)
    facts = build_behavior_facts(store)
    extraction = compile_cards(store, facts, run_facts(store, facts, "run"))
    card = next(c for c in extraction["cards"] if c["kind"] == "model")
    metadata = card_metadata(store, card, extraction["registry"])
    payload = serialized(metadata)
    assert "Inspect orbital calculation" in payload and "rg angular_velocity src" in payload
    assert not any(marker in payload for marker in ("RAW_REASONING", "RAW_PATCH", "RAW_REPORT", "raw_refs"))
    assert len(payload) < 1300
    for field in metadata["fields"]:
        assert store.resolve(extraction["registry"][field["ref"]]["ref"]) == field["text"]


@pytest.mark.asyncio
async def test_unknown_output_value_is_terminal_without_read_or_repair_calls():
    from loom.core import ok
    from loom.llm import LlmResponse, TokenUsage

    class Uncertain(GraphJudge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            payload = json.loads(messages[1].content)
            self.requests.append(payload)
            return ok(LlmResponse(content=json.dumps({"assessments": [dict(
                card_id=c["id"], status="needs_evidence", purpose="Inspect source", delta="Output value is not established",
                contribution="unknown", necessity="unknown", refs=c["refs"][:1],
            ) for c in payload["cards"]], "structure": [dict(decision="single_goal", reason="One source inspection; no independent intermediate result",
                refs=payload["cards"][0]["refs"][:1])]}), usage=TokenUsage(50, 30, 80)))

    store = source(action(1, output={"status": "success", "content": "RAW_TOOL_OUTPUT" * 10000}))
    cp, judge = {}, Uncertain()
    result = await run(store, build_behavior_facts(store), "run", cp, judge)
    assert len(judge.requests) == 1
    assert result["coverage"]["pipeline_complete"]
    assert result["coverage"]["unknown_cards"] == 2
    assert all(a["basis"] == "default_rule" for a in result["action_attributions"])
    assert "RAW_TOOL_OUTPUT" not in serialized(judge.requests)
    assert not cp["read_chars"]
    assert "evidence_requests" not in judge.requests[0]["contract"]["records"]
