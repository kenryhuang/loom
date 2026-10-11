import asyncio
import json
from dataclasses import asdict

import pytest

from loom.core import ok
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.behavior_judge import _validate_focus, judge_behavior
from loom.evaluation.behavior_progress import merge_findings, node_identity, progress_timeline
from loom.evaluation.behavior_sequence import focus_context
from loom.evaluation.evidence_review import EvidenceReview
from loom.evaluation.evidence_store import EvidenceStore
from loom.llm import LlmResponse, TokenUsage


def source(events):
    return EvidenceStore("trace.jsonl", "".join(json.dumps({"run_id": "run", **e}) + "\n" for e in events).encode())


def action(n, tool="read_file", inp=None, output=None):
    scope = {"loop_id": "loop", "trace_id": f"trace{n}", "step_number": n, "llm_call_id": str(n)}
    t = {**scope, "tool_id": tool, "tool_call_id": f"tool{n}"}
    return [
        {"type": "llm.requested", **scope, "messages": [{"role": "user", "content": "Investigate the problem"}]},
        {"type": "llm.completed", **scope, "response": {"content": "Investigate", "usage": {"total_tokens": 15}}},
        {"type": "tool.started", **t, "input": inp or {"path": "a.txt"}},
        {"type": "tool.completed", **t, "output": output if output is not None else {"path": "a.txt", "content": "same", "truncated": False}},
    ]


def test_initial_workflow_content_dependencies_and_temporal_execution():
    events = [{"type": "workflow.node.started", "plan": {"revision": 2, "items": [
        {"id": "read", "content": "Read the source", "status": "in_progress"}]},
        "workflow": {"active": "read", "workflow": {"nodes": [{"id": "read", "dependencies": ["probe"]}]}}},
        *action(1), {"type": "plan.updated", "plan": {"revision": 3, "items": [
            {"id": "read", "content": "Read the source", "status": "completed"},
            {"id": "test", "content": "Test the cause", "status": "in_progress"}]}}, *action(2)]
    facts = build_behavior_facts(source(events))
    plans = facts["plan_revisions"]
    assert plans[0]["revision"] == 2
    assert plans[0]["nodes"][0]["description"] == "Read the source"
    assert plans[0]["nodes"][0]["dependencies"] == ["probe"]
    assert [c["plan_node_id"] for c in facts["model_calls"]] == ["read", "test"]
    rows = facts["sequence"]["plan_execution"]
    assert len(rows) == 2 and len(rows[0]["round_ids"]) == 1
    assert rows[0]["evidenced_done"] == "unknown"
    context = focus_context(facts, facts["segments"][1])
    assert len(context["context_segments"]) == 1
    assert {p["revision"] for p in context["plans"]} == {2, 3}


def test_nested_workflow_objective_and_ambiguous_active_nodes():
    facts = build_behavior_facts(source([{"type": "workflow.revised", "workflow": {"workflow": {"revision": 4, "nodes": [
        {"id": "a", "objective": "First", "status": "running"}, {"id": "b", "objective": "Second", "status": "running"}]}}}, *action(1)]))
    assert facts["plan_revisions"][0]["nodes"][1]["description"] == "Second"
    assert facts["model_calls"][0]["plan_node_id"] is None


@pytest.mark.parametrize("middle,expected", [
    ([], "candidate"),
    (action(2, "write_file", output={"path": "a.txt", "bytes_written": 4}), "needs_context"),
    (action(2, "shell_execute", {"command": "some-program"}, {"stdout": "", "exit_code": 0}), "needs_context"),
    (action(2, "write_file", {"path": "other.txt"}, {"path": "other.txt", "bytes_written": 4}), "candidate"),
])
def test_repeated_read_considers_intervening_writes_and_opaque_shell(middle, expected):
    data = build_behavior_facts(source([*action(1, inp={"path": "./a.txt"}), *middle, *action(3)]))
    repeats = data["sequence"]["repetitions"]
    assert len(repeats) == 1 and repeats[0]["status"] == expected
    assert len(repeats[0]["evidence_refs"]) == 2


def test_parallel_write_is_a_mutation_boundary_even_if_it_completes_later():
    write = action(2, "write_file", output={"path": "a.txt"})
    events = [*action(1), *write[:-1], *action(3), write[-1]]
    repeat = build_behavior_facts(source(events))["sequence"]["repetitions"][0]
    assert repeat["status"] == "needs_context"


def test_new_range_and_changed_content_are_not_repeated():
    facts = build_behavior_facts(source([*action(1), *action(2, output={"path": "a.txt", "content": "same and more"})]))
    assert not facts["sequence"]["repetitions"]


def test_truncated_reads_do_not_establish_whole_file_equality():
    out = {"path": "a.txt", "content": "same", "truncated": True}
    facts = build_behavior_facts(source([*action(1, output=out), *action(2, output=out)]))
    assert facts["sequence"]["repetitions"][0]["status"] == "needs_context"
    assert facts["sequence"]["resources"][0]["version_sha256"] is None


def test_shell_cat_and_file_read_share_recorded_resource_identity():
    facts = build_behavior_facts(source([*action(1), *action(2, "shell_execute", {"command": "cat a.txt"},
                                                                            {"stdout": "same", "exit_code": 0})]))
    assert len(facts["sequence"]["repetitions"]) == 1


@pytest.mark.parametrize("command", [["cat", "a.txt"], [], None, {"argv": ["cat", "a.txt"]}, 42, True])
@pytest.mark.parametrize("failed", [False, True])
def test_non_string_shell_commands_remain_opaque_without_breaking_trace_analysis(command, failed):
    shell = action(2, "shell_execute", {"command": command}, {"stdout": "same", "exit_code": 0})
    if failed:
        shell[-1] = {**shell[-1], "type": "tool.failed", "error": {"message": "Invalid command input"}}
        shell[-1].pop("output")
    facts = build_behavior_facts(source([*action(1), *shell, *action(3)]))
    assert facts["reasoning_activity"]["acquisitions"][1]["classification"] == "unknown"
    assert len(facts["tool_uses"]) == 3
    assert facts["sequence"]["repetitions"][0]["status"] == "needs_context"


def test_repeated_reads_do_not_cross_applied_goal_boundary():
    facts = build_behavior_facts(source([*action(1), {"type": "task.goal.revised", "objective": "A different goal", "input_cursor": 2},
                                        *action(2)]))
    assert not facts["sequence"]["repetitions"]
    assert not facts["candidate_windows"]
    assert not focus_context(facts, facts["segments"][1])["context_segments"]


def test_ten_tools_without_new_recognizable_results_only_nominate_a_window():
    events = []
    for n in range(11):
        events += action(n, "search", {"query": str(n)}, {"results": []})
    facts = build_behavior_facts(source(events))
    windows = [w for w in facts["candidate_windows"] if w["pattern_type"] == "low_progress_window"]
    assert len(windows) == 1 and windows[0]["tool_calls"] == 10
    assert windows[0]["status"] == "candidate"
    assert all(s["semantic_progress"] == "not_assessed" for s in facts["sequence"]["signals"])


def test_progress_requires_delivered_primary_segment_evidence():
    store = source([*action(1), *action(2)])
    facts = build_behavior_facts(store)
    primary = facts["segments"][1]
    review = EvidenceReview(store)
    prior = asdict(store.ref(store.events[3], "output.content"))
    current = asdict(store.ref(store.events[7], "output.content"))
    review.present(prior, max_chars=100)
    review.present(current, max_chars=100)
    payload = {"progress": [{"segment_id": primary["id"], "kind": "information_gain", "label": "New evidence", "evidence_refs": [prior]}]}
    assert not _validate_focus(payload, review, [primary["id"]], facts)["progress"]
    payload["progress"][0]["evidence_refs"] = [current]
    result = _validate_focus(payload, review, [primary["id"]], facts)
    assert result["progress"][0]["line"] == 8
    timeline = progress_timeline(facts, result["progress"])
    assert timeline[0]["since_previous_progress"]["known_tokens"] == 30
    assert timeline[0]["since_previous_progress"]["tool_calls"] == 2


def test_progress_intervals_do_not_double_count_parallel_landmarks_or_rounds():
    store = source([*action(1), *action(2)])
    facts = build_behavior_facts(store)
    milestones = [{"segment_id": s["id"], "kind": "information_gain", "label": str(i), "line": (i + 1) * 4}
                  for i, s in enumerate(facts["segments"])]
    milestones.append({**milestones[-1], "kind": "decision"})
    timeline = progress_timeline(facts, milestones)
    assert sum(r["since_previous_progress"]["known_tokens"] for r in timeline) == 30
    assert sum(r["since_previous_progress"]["tool_calls"] for r in timeline) == 2


def test_graph_identity_and_multidimensional_findings_deduplicate_by_provenance():
    node = {"kind": "evidence", "label": "One fact", "evidence_refs": [{"line_number": 2}]}
    assert node_identity(node) == node_identity({**node, "id": "arbitrary-model-id"})
    assert node_identity(node) != node_identity({**node, "label": "Another fact"})
    base = {"id": "a", "pattern_type": "repeated_unchanged_result", "mechanism_id": "lost_context", "epistemic_status": "inferred",
            "supporting_refs": [{"line_number": 2}], "dimension": "progress_effectiveness", "segment_id": "s1"}
    merged = merge_findings([base, {**base, "id": "b", "dimension": "investigation_efficiency", "segment_id": "s2"}])
    assert len(merged) == 1
    assert merged[0]["segment_ids"] == ["s1", "s2"]
    assert len(merged[0]["dimensions"]) == 2


def test_cross_step_pipeline_saves_progress_without_adding_review_calls():
    class Judge:
        model = "fixture"

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools=None, cancellation=None):
            self.calls += 1
            payload = json.loads(messages[1].content)
            if payload["stage"].startswith("scan:"):
                result = {"candidate_segment_ids": [s["id"] for s in payload["segment_index"]], "control_segment_ids": []}
            elif payload["stage"].startswith("focus:"):
                primary = payload["segments"][0]
                assert len(payload["context_segments"]) == 1
                assert primary["id"] != payload["context_segments"][0]["id"]
                line = max(r["line_number"] for r in primary["evidence_refs"])
                ref = next(p["returned_ref"] for p in payload["evidence_packets"] if p["returned_ref"]["line_number"] == line)
                result = {"progress": [{"segment_id": primary["id"], "kind": "information_gain", "label": "Relevant observation",
                                        "evidence_refs": [ref]}],
                          "assessments": [{"segment_id": primary["id"], "dimension": "plan_quality", "status": "not_applicable",
                                           "rationale": "No formal plan", "evidence_refs": []}],
                          "plan_assessments": [{"segment_id": primary["id"], "plan_snapshot_id": None, "node_id": None,
                                                "status": "unknown", "rationale": "No formal plan", "evidence_refs": []}]}
                assert not payload["plans"]
                assert any("return plan_assessments: []" in text for text in payload["context_limitations"])
            else:
                result = {"summary": "Two source-backed landmarks", "finding_ids": [], "limitations": []}
            return ok(LlmResponse(content=json.dumps(result), usage=TokenUsage(10, 5, 15)))

    store = source([*action(1), *action(2)])
    facts, judge, checkpoint = build_behavior_facts(store), Judge(), {}
    result = asyncio.run(judge_behavior(store, facts, judge, checkpoint=checkpoint)).unwrap()
    assert judge.calls == 4
    assert len(result["progress"]) == 2
    assert result["coverage"]["attempted_segments"] == 2
    assert result["plan_assessments"] == []
    assert len([s for s in result["coverage"]["limitations"] if "invalid optional plan" in s]) == 2
    assert checkpoint["repairs"] == 0
    assert any("invalid behavior assessment" in s for s in result["coverage"]["limitations"])
    assert sum(p["since_previous_progress"]["known_tokens"] for p in result["progress"]) == 30
    assert asyncio.run(judge_behavior(store, facts, judge, checkpoint=checkpoint)).unwrap()["progress"] == result["progress"]
    assert judge.calls == 4


def test_future_plan_cannot_be_used_to_judge_earlier_actions():
    def plan(label):
        return {"type": "plan.updated", "plan": {"items": [{"id": "n", "content": label, "status": "in_progress"}]}}

    store = source([plan("Inspect first"), *action(1), plan("Changed requirement")])
    facts, review = build_behavior_facts(store), EvidenceReview(store)
    primary = facts["segments"][0]
    ref = asdict(store.ref(store.events[4], "output.content"))
    review.present(ref, max_chars=100)
    value = {"plan_assessments": [{"segment_id": primary["id"], "plan_snapshot_id": facts["plan_revisions"][-1]["id"],
                                   "node_id": "n", "status": "unjustified_deviation", "rationale": "Does not match later plan",
                                   "evidence_refs": [ref]}]}
    assert _validate_focus(value, review, [primary["id"]], facts)["plan_assessments"][0]["status"] == "unknown"


@pytest.mark.parametrize(("field", "value"), [
    ("plan_snapshot_id", "fabricated"), ("plan_snapshot_id", None), ("plan_snapshot_id", []),
    ("node_id", "missing"), ("node_id", {}), ("segment_id", "neighbor"),
    ("status", "completed"), ("status", []), ("rationale", None),
])
def test_invalid_optional_plan_row_preserves_valid_plan_and_other_findings(field, value):
    store = source([{"type": "plan.updated", "plan": {"items": [{"id": "n", "content": "Inspect", "status": "in_progress"}]}},
                    *action(1)])
    facts, review = build_behavior_facts(store), EvidenceReview(store)
    primary = facts["segments"][0]
    ref = review.present(asdict(store.ref(store.events[4], "output.content")), max_chars=100)["returned_ref"]
    valid = {"segment_id": primary["id"], "plan_snapshot_id": facts["plan_revisions"][0]["id"], "node_id": "n",
             "status": "followed", "rationale": "Inspection result", "evidence_refs": [ref]}
    result = _validate_focus({"plan_assessments": [{**valid, field: value}, valid],
                              "preserved_behaviors": [{"behavior": "Inspected file", "evidence_refs": [ref]}]},
                             review, [primary["id"]], facts)
    assert len(result["preserved_behaviors"]) == 1
    assert len(result["plan_assessments"]) == 1
    assert result["plan_assessments"][0]["status"] == "followed"
    assert field in result["validation_issues"][0]["invalid_fields"]
    assert result["limitations"]


def test_invalid_optional_plan_row_cannot_bypass_source_validation():
    store = source(action(1))
    facts, review = build_behavior_facts(store), EvidenceReview(store)
    ref = {**asdict(store.ref(store.events[3], "output.content")), "source_sha256": "fabricated"}
    with pytest.raises(ValueError):
        _validate_focus({"plan_assessments": [{"status": "unknown", "evidence_refs": [ref]}]},
                        review, [facts["segments"][0]["id"]], facts)


@pytest.mark.parametrize(("field", "value"), [
    ("segment_id", "neighbor"), ("segment_id", []), ("dimension", "planning"), ("dimension", {}),
    ("status", "not_applicable"), ("status", []), ("rationale", None),
])
def test_invalid_behavior_assessment_is_unknown_without_losing_other_dimensions(field, value):
    store = source(action(1))
    facts, review = build_behavior_facts(store), EvidenceReview(store)
    sid = facts["segments"][0]["id"]
    ref = review.present(asdict(store.ref(store.events[3], "output.content")), max_chars=100)["returned_ref"]
    row = {"segment_id": sid, "dimension": "plan_quality", "status": "effective", "rationale": "Supported", "evidence_refs": [ref]}
    result = _validate_focus({"assessments": [{**row, field: value}, {**row, "dimension": "investigation_efficiency"}]},
                             review, [sid], facts)
    assert len(result["assessments"]) == 5
    by_dimension = {a["dimension"]: a for a in result["assessments"]}
    assert by_dimension["plan_quality"]["status"] == "unknown"
    assert by_dimension["investigation_efficiency"]["status"] == "effective"
    assert field in result["validation_issues"][0]["invalid_fields"]
    assert result["limitations"]


def test_new_semantic_analysis_rebuilds_old_cached_facts_without_changing_old_jobs():
    from loom.evaluation.behavior_contracts import ANALYZER_VERSION
    from loom.service.semantic import _behavior_facts

    store = source(action(1))
    old = {"behavior": {"analyzer_version": "behavior-evaluation.3.4", "plan_revisions": []}}
    updated = _behavior_facts(store, old, {"analyzer_version": ANALYZER_VERSION})
    assert updated["sequence"]["resources"]
    assert _behavior_facts(store, old, {"analyzer_version": "behavior-evaluation.3.4"}) is old["behavior"]
