import json

import pytest


def make_store(tmp_path):
    from loom.evaluation.evidence_store import EvidenceStore

    path = tmp_path / "trace.jsonl"
    path.write_text('\n' + json.dumps({"type": "llm.completed", "run_id": "r", "loop_id": "l", "trace_id": "t",
                                        "step_number": 0, "llm_call_id": "c", "response": {"content": "begin\ncritical ending"}}) + '\n')
    return EvidenceStore.open(path)


def test_hashless_evidence_preserves_physical_line_and_can_expand_beyond_preview(tmp_path):
    store = make_store(tmp_path)
    ref = store.ref(store.events[0], "response.content")
    assert ref.line_number == 2
    assert ref.event_hash is None
    preview = store.read(ref, max_chars=5)
    assert preview["content"] == "begin"
    assert preview["truncated"] is True
    assert store.resolve(preview["returned_ref"]) == "begin"
    assert preview["returned_ref"]["end"] == 5
    assert store.resolve(ref) == "begin\ncritical ending"


def test_evidence_ref_rejects_wrong_source_missing_field_and_out_of_bounds(tmp_path):
    from dataclasses import replace

    store = make_store(tmp_path)
    ref = store.ref(store.events[0], "response.content")
    with pytest.raises(ValueError, match="source"):
        store.resolve(replace(ref, source_sha256="other"))
    with pytest.raises(ValueError, match="field"):
        store.ref(store.events[0], "response.missing")
    with pytest.raises(ValueError, match="range"):
        store.ref(store.events[0], "response.content", start=200)
    with pytest.raises(ValueError, match="range"):
        store.resolve(replace(ref, start=-1, end=4))
    with pytest.raises(ValueError, match="hash"):
        store.resolve(replace(ref, event_hash="invented"))


def test_evidence_uses_snapshot_and_does_not_follow_paths_in_trace(tmp_path):
    store = make_store(tmp_path)
    ref = store.ref(store.events[0], "response.content", start=6, end=14)
    store.path.write_text("changed")
    assert store.resolve(ref) == "critical"
    assert store.read(ref, max_chars=50)["truncated"] is False
    preview = store.read(ref, max_chars=3)
    assert store.resolve(preview["returned_ref"]) == "cri"
    assert preview["returned_ref"]["start"] == 6
    assert preview["returned_ref"]["end"] == 9


def test_corrupt_and_duplicate_records_have_explicit_coverage(tmp_path):
    from loom.evaluation.evidence_store import EvidenceStore

    p = tmp_path / "trace.jsonl"
    row = {"hash": "incorrect", "payload": {"type": "run.started", "run_id": "r"}}
    p.write_text(json.dumps(row) + '\n' + json.dumps(row) + '\n')
    store = EvidenceStore.open(p)
    assert len(store.events) == 2
    assert store.coverage["hash_mismatches"] == [1, 2]
    assert store.coverage["repeated_record_lines"] == [2]
    p.write_text('{bad}\n')
    with pytest.raises(ValueError, match="line 1"):
        EvidenceStore.open(p)


def test_diagnosis_validation_rejects_unresolvable_evidence_and_unobserved_facts(tmp_path):
    from dataclasses import asdict

    from loom.evaluation.diagnostics import parse_diagnosis

    store = make_store(tmp_path)
    ref = asdict(store.ref(store.events[0], "response.content"))
    value = {"dimension": "context_effectiveness", "scope": "run:r", "observation": "A complete response was recorded",
             "interpretation": "No task supplied", "epistemic_status": "observed", "supporting_refs": [ref]}
    assert parse_diagnosis(value, store).supporting_refs[0].line_number == 2
    with pytest.raises(ValueError, match="evidence"):
        parse_diagnosis({**value, "supporting_refs": []}, store)
    with pytest.raises(ValueError, match="source"):
        parse_diagnosis({**value, "supporting_refs": [{**ref, "source_sha256": "fake"}]}, store)
    with pytest.raises(ValueError, match="dimension"):
        parse_diagnosis({**value, "dimension": "quality_score"}, store)
    with pytest.raises(ValueError, match="confidence"):
        parse_diagnosis({**value, "confidence": float("nan")}, store)


def test_omitted_optional_hash_is_valid_only_for_hashless_records(tmp_path):
    from loom.evaluation.evidence_store import EvidenceStore

    store = make_store(tmp_path)
    pointer = {"source_sha256": store.source_sha256, "line_number": 2, "field_path": "response.content"}
    assert store.resolve(pointer) == "begin\ncritical ending"
    path = tmp_path / "hashed.jsonl"
    path.write_text(json.dumps({"hash": "recorded-hash", "payload": {"type": "run.started", "run_id": "r"}}))
    hashed = EvidenceStore.open(path)
    with pytest.raises(ValueError, match="hash"):
        hashed.resolve({"source_sha256": hashed.source_sha256, "line_number": 1})
