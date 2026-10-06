"""Per-round semantic summaries remain constrained by reviewed source evidence."""

import copy
import json
from dataclasses import asdict

import pytest

from loom.evaluation.diagnostics import DIMENSIONS, FactAnalysis
from loom.evaluation.evidence_review import EvidenceReview
from loom.evaluation.evidence_store import EvidenceStore


def setup_review(tmp_path):
    rows = [
        {"type": "run.started", "run_id": "r", "metadata": {"objective": "Find target"}},
        {"type": "llm.requested", "run_id": "r", "loop_id": "l", "messages": [{"role": "user", "content": "Find target"}]},
        {"type": "tool.completed", "run_id": "r", "loop_id": "l", "output": {"content": "Target located at the end of this result"}},
        {"type": "tool.completed", "run_id": "r", "loop_id": "other", "output": "foreign loop"},
        {"type": "tool.completed", "run_id": "other", "loop_id": "l", "output": "foreign run"},
    ]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    store = EvidenceStore.open(path)
    pointers = [asdict(store.ref(event)) for event in store.events]
    facts = FactAnalysis(trajectory=({"id": "round-1", "run_id": "r", "loop_id": "l", "request_ref": pointers[1]},))
    review = EvidenceReview(store)
    return store, facts, review, pointers


def round_value(refs, *, progress="information_gain", dimension_status="effective"):
    return {"round_id": "round-1", "pre_state": "Target location unknown", "intent": "Find target",
            "action": "Search recorded input", "observed_change": "Search result gives location", "post_state": "Location available",
            "progress_kind": progress, "evidence_refs": refs,
            "dimensions": {name: {"status": dimension_status, "rationale": "Recorded evidence supports this assessment", "evidence_refs": refs}
                           for name in DIMENSIONS}}


def test_reviewed_round_splits_decision_time_and_hindsight_refs(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    for ref in refs[:3]:
        review.present(ref, max_chars=10000)
    value = round_value(refs[:3])
    value["decision_time_refs"] = [refs[2]]  # The model cannot supply its own chronology.
    (row,) = validate_round_analyses([value], store, facts, review)
    assert row["progress_kind"] == "information_gain"
    assert row["prior_record_refs"] == [refs[0]]
    assert row["decision_time_refs"] == [refs[1]]
    assert row["hindsight_refs"] == [refs[2]]
    assert row["dimensions"]["loop_progress"]["prior_record_refs"] == [refs[0]]
    assert row["dimensions"]["loop_progress"]["decision_time_refs"] == [refs[1]]
    assert row["dimensions"]["loop_progress"]["hindsight_refs"] == [refs[2]]
    json.dumps(row)


def test_unread_long_evidence_downgrades_round_and_dimension_until_expanded(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, _ = setup_review(tmp_path)
    ref = asdict(store.ref(store.events[2], "output.content"))
    value = round_value([ref])
    review.present(ref, max_chars=6)
    (row,) = validate_round_analyses([value], store, facts, review)
    assert row["progress_kind"] == "unknown"
    assert row["limitation"]
    assert all(d["status"] == "unknown" and d["limitation"] for d in row["dimensions"].values())
    review.present(ref, max_chars=10000)
    (row,) = validate_round_analyses([value], store, facts, review)
    assert row["progress_kind"] == "information_gain"
    assert row["dimensions"]["context_effectiveness"]["status"] == "unknown"
    assert all(d["status"] == "effective" for name, d in row["dimensions"].items() if name != "context_effectiveness")


def test_each_dimension_requires_its_own_reviewed_evidence(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    review.present(refs[1], max_chars=10000)
    value = round_value([refs[1]])
    value["dimensions"]["tool_effectiveness"]["evidence_refs"] = [refs[2]]
    (row,) = validate_round_analyses([value], store, facts, review)
    assert row["progress_kind"] == "information_gain"
    assert row["dimensions"]["context_effectiveness"]["status"] == "effective"
    assert row["dimensions"]["tool_effectiveness"]["status"] == "unknown"


@pytest.mark.parametrize("ref_index", [3, 4])
def test_cross_run_or_loop_evidence_is_rejected_even_when_delivered(tmp_path, ref_index):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    review.present(refs[ref_index], max_chars=10000)
    with pytest.raises(ValueError, match="scope"):
        validate_round_analyses([round_value([refs[ref_index]])], store, facts, review)


def test_missing_refs_remain_unknown_and_omitted_rounds_are_allowed(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, _ = setup_review(tmp_path)
    assert validate_round_analyses([], store, facts, review) == ()
    (row,) = validate_round_analyses([round_value([])], store, facts, review)
    assert row["progress_kind"] == "unknown"
    assert all(d["status"] == "unknown" for d in row["dimensions"].values())


def test_invented_round_dimension_duplicate_and_invalid_state_are_rejected(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    value = round_value([refs[1]])
    cases = [([{**value, "round_id": "invented"}], "round"), ([value, value], "duplicate"),
             ([{**value, "pre_state": 42}], "pre_state"), ([{**value, "progress_kind": "success"}], "progress_kind")]
    extra = copy.deepcopy(value)
    extra["dimensions"]["quality"] = extra["dimensions"]["loop_progress"]
    cases.append(([extra], "dimension"))
    missing = copy.deepcopy(value)
    del missing["dimensions"]["verify_gate"]
    cases.append(([missing], "dimension"))
    for values, message in cases:
        with pytest.raises(ValueError, match=message):
            validate_round_analyses(values, store, facts, review)


def test_missing_request_never_labels_evidence_as_decision_time(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    facts = FactAnalysis(trajectory=({**facts.trajectory[0], "request_ref": None},))
    review.present(refs[0], max_chars=10000)
    (row,) = validate_round_analyses([round_value([refs[0]])], store, facts, review)
    assert row["decision_time_refs"] == []
    assert row["prior_record_refs"] == []
    assert row["hindsight_refs"] == [refs[0]]
    assert row["temporal_limitation"]


def test_earlier_tool_result_not_injected_is_prior_record_not_decision_evidence(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"type": "tool.completed", "run_id": "r", "loop_id": "l", "output": {"content": "Secret result not injected"}},
        {"type": "llm.requested", "run_id": "r", "loop_id": "l", "messages": [{"role": "user", "content": "Find target"}]},
    ]))
    store = EvidenceStore.open(path)
    prior, request = [asdict(store.ref(event)) for event in store.events]
    facts = FactAnalysis(trajectory=({"id": "round-1", "run_id": "r", "loop_id": "l", "request_ref": request},))
    review = EvidenceReview(store)
    review.present(prior, max_chars=10000)
    review.present(request, max_chars=10000)
    (row,) = validate_round_analyses([round_value([prior, request])], store, facts, review)
    assert row["prior_record_refs"] == [prior]
    assert row["decision_time_refs"] == [request]
    assert row["hindsight_refs"] == []
    assert row["temporal_limitation"]
    assert all(d["prior_record_refs"] == [prior] and d["decision_time_refs"] == [request] for d in row["dimensions"].values())


def test_unread_request_ref_is_not_labelled_delivered_decision_evidence(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    (row,) = validate_round_analyses([round_value([refs[1]])], store, facts, review)
    assert row["decision_time_refs"] == []
    assert row["unreviewed_request_refs"] == [refs[1]]
    review.present(refs[1], max_chars=10000)
    (row,) = validate_round_analyses([round_value([refs[1]])], store, facts, review)
    assert row["decision_time_refs"] == [refs[1]]
    assert row["unreviewed_request_refs"] == []


def test_prior_only_evidence_cannot_establish_current_round_progress_or_effectiveness(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    review.present(refs[0], max_chars=10000)
    (row,) = validate_round_analyses([round_value([refs[0]])], store, facts, review)
    assert row["progress_kind"] == "unknown"
    assert row["epistemic_status"] == "unknown"
    assert row["prior_record_refs"] == [refs[0]]
    assert row["limitation"]
    assert all(d["status"] == "unknown" and d["limitation"] for d in row["dimensions"].values())


def test_later_tool_result_can_show_progress_but_not_request_context_effectiveness(tmp_path):
    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    review.present(refs[2], max_chars=10000)
    (row,) = validate_round_analyses([round_value([refs[2]])], store, facts, review)
    assert row["progress_kind"] == "information_gain"
    assert row["dimensions"]["tool_effectiveness"]["status"] == "effective"
    context = row["dimensions"]["context_effectiveness"]
    assert context["status"] == "unknown"
    assert context["epistemic_status"] == "unknown"
    assert context["limitation"]
    assert context["hindsight_refs"] == [refs[2]]


def test_unique_linked_resumed_tool_allows_cross_loop_evidence_but_not_cross_run(tmp_path):
    from dataclasses import replace

    from loom.evaluation.round_review import validate_round_analyses

    store, facts, review, refs = setup_review(tmp_path)
    facts = replace(facts, tool_uses=({"round_id": "round-1", "evidence_refs": [refs[3], refs[4]]},))
    for ref in refs[3:]:
        review.present(ref, max_chars=10000)
    (row,) = validate_round_analyses([round_value([refs[3]])], store, facts, review)
    assert row["progress_kind"] == "information_gain"
    assert row["dimensions"]["context_effectiveness"]["status"] == "unknown"
    with pytest.raises(ValueError, match="scope"):
        validate_round_analyses([round_value([refs[4]])], store, facts, review)
