import json
from dataclasses import asdict

from loom.evaluation.diagnostics import FactAnalysis
from loom.evaluation.evidence_store import EvidenceStore


def fixture():
    from pathlib import Path

    rows = [{"type": "llm.requested", "run_id": "r", "messages": [{"role": "user", "content": "x" * 400}]},
            {"type": "tool.completed", "run_id": "r", "output": {"content": "historical source"}},
            {"type": "tool.completed", "run_id": "r", "output": {"results": ["orphan search result"]}}]
    store = EvidenceStore(Path("memory.jsonl"), "\n".join(json.dumps(r) for r in rows).encode())
    refs = [asdict(store.ref(e)) for e in store.events]
    return store, refs


def test_context_navigation_avoids_repeated_units_but_preserves_removed_evidence():
    from loom.evaluation.judge_navigation import batch_payload

    store, refs = fixture()
    unit = {"id": "u", "ref": refs[0], "excerpt": "x" * 320, "char_length": 400}
    removed = {"id": "old", "ref": refs[1], "excerpt": "historical source"}
    round_row = {"id": "round:r", "run_id": "r"}
    facts = FactAnalysis(trajectory=(round_row,), context_deltas=({"round_id": "round:r", "units": [unit],
                        "added": [], "retained": [unit], "removed": [removed]},))
    payload = batch_payload(store, facts, (round_row,))
    delta = payload["context_deltas"][0]
    assert delta["retained_count"] == 1
    assert delta["retained_ids"] == ["u"]
    assert "retained" not in delta
    assert delta["units"][0]["excerpt"] == "x" * 120
    assert delta["units"][0]["excerpt_truncated"] is True
    assert store.resolve(delta["removed"][0]["ref"]) == store.resolve(refs[1])


def test_tool_navigation_labels_selected_injections_and_indexes_orphan_evidence():
    from loom.evaluation.judge_navigation import batch_payload

    store, refs = fixture()
    round_row = {"id": "round:r", "run_id": "r"}
    injections = [{"round_id": f"next:{i}", "ref": refs[0]} for i in range(8)]
    facts = FactAnalysis(trajectory=(round_row,), tool_uses=(
        {"id": "tool:linked", "run_id": "r", "round_id": "round:r", "injections": injections, "evidence_refs": [refs[1]]},
        {"id": "tool:orphan", "run_id": "r", "round_id": None, "tool_id": "search", "evidence_refs": [refs[2]]}))
    payload = batch_payload(store, facts, (round_row,))
    tool = payload["tool_uses"][0]
    assert tool["injection_count"] == 8
    assert [r["round_id"] for r in tool["injections"]] == ["next:0", "next:1", "next:7"]
    assert tool["injections_omitted"] == 5
    assert "first two and last" in tool["injection_selection"]
    orphan = next(r for r in payload["tool_index"] if r["id"] == "tool:orphan")
    assert store.resolve(orphan["ref"]) == store.resolve(refs[2])
    assert payload["navigation"]["tool_details_omitted"] == 1


def test_nonlocal_verification_stays_expandable_without_full_run_outputs():
    from loom.evaluation.judge_navigation import batch_payload

    store, refs = fixture()
    round_row = {"id": "round:r", "run_id": "r"}
    task = {"id": "task:r", "run_id": "r", "objective": "Keep the original task", "evidence_refs": [refs[0]]}
    facts = FactAnalysis(task_contracts=(task,), trajectory=(round_row,), verification_evidence=(
        {"id": "verify:local", "run_id": "r", "round_id": "round:r", "kind": "command_execution", "evidence_refs": [refs[0]]},
        {"id": "verify:other", "run_id": "r", "round_id": "later", "kind": "recorded_content", "line_number": 2,
         "evidence_refs": [refs[1]], "expensive_output": "not initial evidence" * 10000}))
    payload = batch_payload(store, facts, (round_row,))
    assert payload["task_contracts"] == [task]
    assert [r["id"] for r in payload["verification_evidence"]] == ["verify:local"]
    indexed = payload["verification_index"][0]
    assert indexed["id"] == "verify:other"
    assert store.resolve(indexed["ref"]) == store.resolve(refs[1])
    assert "expensive_output" not in json.dumps(payload)
    assert payload["navigation"]["verification_details_omitted"] == 1


def test_empty_batch_keeps_no_round_task_and_orphan_tools_visible():
    from loom.evaluation.judge_navigation import batch_payload

    store, refs = fixture()
    task = {"id": "task:r", "run_id": "r", "criteria": [], "evidence_refs": [refs[0]]}
    facts = FactAnalysis(task_contracts=(task,), tool_uses=({"id": "orphan", "run_id": "r", "round_id": None,
                                                         "evidence_refs": [refs[2]], "injections": []},))
    payload = batch_payload(store, facts, ())
    assert payload["task_contracts"] == [task]
    assert payload["tool_uses"][0]["id"] == "orphan"
    assert payload["navigation"]["original_evidence_access"] == "read_evidence"


def test_orphan_index_exposes_result_and_input_as_separately_expandable_evidence():
    from loom.evaluation.judge_navigation import batch_payload

    store, refs = fixture()
    facts = FactAnalysis(tool_uses=({"id": "orphan", "round_id": None, "input_ref": refs[0],
                                    "raw_output_ref": refs[2], "evidence_refs": [refs[0], refs[2]]},))
    payload = batch_payload(store, facts, ({"id": "round:r", "run_id": "r"},))
    indexed = payload["tool_index"][0]
    assert store.resolve(indexed["ref"]) == store.resolve(refs[2])
    assert store.resolve(indexed["input_ref"]) == store.resolve(refs[0])
