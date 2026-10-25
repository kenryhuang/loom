"""Contract, isolation and recovery invariants for the current Deep Eval engine."""

import json
from copy import deepcopy

import pytest

from loom.evaluation.goal_loops.checkpoint_repair import reconcile_checkpoint
from loom.evaluation.goal_loops.graph_updates import commit_updates, parse_updates
from loom.evaluation.goal_loops.judge_metadata import terminal_metadata_records
from loom.evaluation.issues import RuleViolation, failure_state, issue, validate_schema
from loom.evaluation.memory.checkpoint import prepare_replay
from loom.evaluation.memory.contracts import protocol_for
from loom.evaluation.memory.records import commit_records
from loom.evaluation.repair import begin_cycle, retry
from loom.llm.api import LlmResponse
from tests.evaluation.test_graph_updates import fixture
from tests.evaluation.test_incremental_graph import assessment
from tests.evaluation.test_metadata_recovery import saved_failure
from tests.evaluation.test_trace_memory import boundary_memory_case


def memory_state():
    return dict(records={}, identity=dict(source="source", run_id="run", graph_revision=1))


def test_schema_error_is_deterministic_and_does_not_echo_rejected_values():
    schema = dict(type="object", properties=dict(title=dict(type="string", maxLength=4), secret=dict(type="integer")))
    first = dict(title="LONG_PRIVATE_TEXT", secret="PRIVATE_TOKEN")
    errors = []
    for row in (first, dict(reversed(list(first.items())))):
        with pytest.raises(RuleViolation) as caught:
            validate_schema(row, schema)
        errors.append(caught.value.issue)
    assert errors[0] == errors[1]
    assert errors[0]["code"] == "schema.type"
    assert errors[0]["path"] == ["secret"]
    assert "PRIVATE" not in json.dumps(errors)


def test_literal_citation_failure_has_location_and_valid_sibling_commits_once():
    unit, good = boundary_memory_case()
    bad = deepcopy(good)
    bad["title"] = "Unsupported claim"
    bad["citations"][0]["quote"] = "This is not the delivered output"
    body = json.dumps(dict(contributions=[], facts=[good, bad], lessons=[], workflows=[]))
    memory = memory_state()
    errors = commit_records(memory, unit, body)
    assert len(errors) == 1 and len(memory["records"]) == 1
    error = errors[0]
    assert (error["code"], error["stage"], error["unit_id"], error["record_type"], error["record_index"], error["path"]) == (
        "memory.citation_not_literal", "memory", unit["id"], "facts", 2, ["citations", 0, "quote"])
    assert error["evidence_id"] == bad["citations"][0]["ref"]
    accepted = deepcopy(memory["records"])
    assert commit_records(memory, unit, body) == errors
    assert memory["records"] == accepted


@pytest.mark.parametrize("mode,kind", [("extract", "workflows"), ("extract", "contributions"), ("synthesis", "facts")])
def test_memory_host_enforces_same_mode_and_empty_scope_limits_as_request(mode, kind):
    unit, row = boundary_memory_case()
    unit["mode"] = mode
    body = dict(contributions=[], facts=[], lessons=[], workflows=[])
    body[kind] = [row]
    memory = memory_state()
    schema = protocol_for(unit).schema
    assert schema["properties"][kind]["maxItems"] == 0
    errors = commit_records(memory, unit, json.dumps(body))
    assert errors[0]["code"] == "schema.maxItems"
    assert errors[0]["record_type"] == kind
    assert errors[0]["details"] == dict(constraint="maxItems", limit=0, actual_count=1)
    assert not memory["records"]


def test_replay_uses_original_request_contract_not_current_repair_contract():
    unit, _ = boundary_memory_case()
    unit["scope"]["stages"] = [dict(id="s", goal_id="goal")]
    original = deepcopy(protocol_for(unit).schema)
    row = dict(stage_id="s", goal_id="goal", rating="unknown", rationale="No observation", key_action_ids=[], citations=[])
    response = json.dumps(dict(contributions=[row], facts=[], lessons=[], workflows=[]))
    memory = memory_state()
    assert not commit_records(memory, unit, response)
    assert protocol_for(unit).schema["properties"]["contributions"]["maxItems"] == 0
    cp = dict(responses=[dict(activity="memory", batch_id=unit["id"], text=response, response_schema=original)])
    prepare_replay(cp, unit, "new-validator")
    assert unit["pending_schema"] == original
    accepted = deepcopy(memory["records"])
    assert not commit_records(memory, unit, unit["pending_response"], schema=unit["pending_schema"])
    assert memory["records"] == accepted


def test_internal_error_is_not_converted_to_model_feedback(monkeypatch):
    unit, row = boundary_memory_case()

    def broken(*args):
        raise KeyError("broken host invariant")

    monkeypatch.setattr("loom.evaluation.memory.records.validate_record", broken)
    with pytest.raises(KeyError, match="broken host invariant"):
        commit_records(memory_state(), unit, json.dumps(dict(contributions=[], facts=[row], lessons=[], workflows=[])))
    _, _, cp, extraction = fixture()
    monkeypatch.setattr("loom.evaluation.goal_loops.graph_updates.commit_group", broken)
    # The callable must accept named arguments to reach the simulated host error.
    monkeypatch.setattr("loom.evaluation.goal_loops.graph_updates.commit_group", lambda *a, **kw: broken())
    with pytest.raises(KeyError, match="broken host invariant"):
        commit_updates(cp, [("assessments", assessment(extraction["cards"][0], stage=""))], batch_id="b",
                       cards=extraction["cards"], delivered=extraction["registry"], cutoff=100,
                       target_ids={c["id"] for c in extraction["cards"]})


def test_retry_has_no_budget_fallthrough_and_resume_is_explicit():
    unit = {}
    begin_cycle(unit)
    errors = [issue("schema.maxLength", "Any wording", category="format"), issue("graph.cards_pending", "Missing", category="coverage")]
    assert retry(unit, errors) and retry(unit, errors)
    assert not retry(unit, errors)
    assert unit["repair_state"]["counts"] == dict(format=2, semantic=0)
    assert unit["repair_state"]["attempts"] == 2
    begin_cycle(unit)
    assert unit["repair_history"][0]["attempts"] == 2
    assert retry(unit, errors)


def test_mixed_failures_charge_both_budgets_but_coverage_is_deferred():
    unit = {}
    begin_cycle(unit)
    errors = [issue("schema.type", "format", category="format"), issue("memory.scope", "scope"),
              issue("memory.stages_pending", "pending", category="coverage")]
    assert retry(unit, errors)
    assert unit["repair_state"]["counts"] == dict(format=1, semantic=1)
    assert not retry(unit, list(reversed(errors)))
    assert not retry(unit, [issue("runtime.internal", "budget reached", category="internal")])


def test_status_selection_uses_categories_not_english_messages():
    assert failure_state([issue("budget.request_chars", "请求过大", category="budget")]) == "budget_exhausted"
    assert failure_state([issue("runtime.internal", "budget reached", category="internal")]) == "failed"
    assert failure_state([issue("memory.scope", "引用错误")]) == "incomplete"


def test_reconciliation_does_not_erase_invalid_structure_record_when_coverage_is_resolved():
    _, _, cp, _, _ = saved_failure()
    error = issue("schema.maxLength", "Reason too long", category="format", record_type="structure", record=0, path=("reason",))
    cp["units"]["b"]["feedback"].append(error)
    reconcile_checkpoint(cp, cp["extraction"]["cards"])
    assert not cp["units"]["b"]["complete"]
    assert [e["code"] for e in cp["conflicts"]] == ["schema.maxLength"]


def test_narrow_graph_request_rejects_extra_types_without_losing_record_location():
    _, _, cp, extraction = fixture()
    card = extraction["cards"][0]
    errors, _ = commit_updates(cp, [("assessments", assessment(card)), ("structure", dict(decision="single_goal", reason="x" * 601, refs=[]))],
                              batch_id="b", cards=extraction["cards"], delivered=extraction["registry"], cutoff=100,
                              target_ids={card["id"]}, allowed_fields={"structure"})
    assert [(e["code"], e["record_index"]) for e in errors] == [("response.record_type", 1), ("schema.maxLength", 2)]
    assert not cp["assessments"]


@pytest.mark.parametrize("value", [dict(type=[]), dict(type={}), dict(groups=[dict(groups=[])]), dict(assessments={})])
def test_malformed_graph_envelopes_are_wire_errors_not_host_exceptions(value):
    records, errors = parse_updates(LlmResponse(content=json.dumps(value)))
    assert not records
    assert errors and all(e["phase"] == "decode" and e["category"] == "format" for e in errors)


@pytest.mark.parametrize("kind,field", [("goals", "key"), ("assessments", "card_id")])
def test_invalid_identity_cannot_crash_pre_schema_normalization(kind, field):
    _, _, cp, extraction = fixture()
    raw = {field: [], "status": "reviewed", "refs": []}
    records = terminal_metadata_records([(kind, raw)])
    errors, _ = commit_updates(cp, records, batch_id="b", cards=extraction["cards"], delivered={}, cutoff=100, target_ids=set())
    assert errors[0]["code"] == "schema.type" and errors[0]["path"] == [field]


def test_refusal_and_truncation_are_not_accepted_as_memory_even_with_valid_json():
    unit, row = boundary_memory_case()
    body = json.dumps(dict(contributions=[], facts=[row], lessons=[], workflows=[]))
    for finish, code, category in (("refusal", "response.refused", "provider"), ("length", "response.truncated", "format")):
        memory = memory_state()
        errors = commit_records(memory, unit, body, finish_reason=finish)
        assert errors[0]["code"] == code and errors[0]["category"] == category
        assert not memory["records"]


def test_graph_truncated_output_cannot_bypass_response_size_limit():
    line = json.dumps(dict(type="structure", decision="single_goal", reason="x", refs=[]))
    records, errors = parse_updates(LlmResponse(content=line + "\n" + " " * 1_000_000, finish_reason="length"))
    assert not records and errors[0]["code"] == "response.response_too_large"
