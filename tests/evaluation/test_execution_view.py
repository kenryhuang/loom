import json
from copy import deepcopy

from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.execution_view import execution_view, time_partition
from loom.evaluation.goal_loops.facts import run_facts
from loom.evaluation.goal_loops.review import initial_state, project
from loom.service.contracts import canonical
from tests.evaluation.test_behavior_sequence import action, source


def sample():
    events = action(1) + action(2) + action(3)
    times = [0, 10, 10, 12, 14, 20, 21, 22, 25, 40, 40, 41]
    for event, second in zip(events, times, strict=True):
        event["at"] = f"2026-10-18T00:00:{second:02d}.000Z"
    for i in range(3):
        events[i * 4 + 1]["response"]["tool_calls"] = [{"id": f"tool{i + 1}", "function": {"name": "read_file", "arguments": "{}"}}]
        events[i * 4 + 1]["response"]["reasoning_content"] = "hidden must not leak"
        events[i * 4 + 1]["response"]["content"] = f"Need evidence for file {i + 1}"
        events[i * 4 + 1]["response"]["stream_metrics"] = {"http_receive_ms": 9000, "delta_emit_ms": 20}
    store = source(events)
    facts = build_behavior_facts(store)
    view = run_facts(store, facts, "run")
    state = initial_state(view)
    state["attempts"] = [dict(id="a", goal_id=state["goals"][0]["id"], evidence_refs=[], approach="Read files and verify")]
    first = view["atoms"][0]["id"]
    state["phases"] = [
        dict(
            id="p",
            attempt_id="a",
            kind="investigation",
            question="Which file explains the behavior?",
            start_line=2,
            end_line=8,
            evidence_refs=[],
            call_ids=[first],
        )
    ]
    return store, project(store, facts, "run", {"entities": state})


def test_time_partition_handles_nested_parallel_calls_gaps_and_unknown_intervals():
    def call(kind, start, end):
        return dict(kind=kind, started_at=f"2026-10-18T00:00:{start:02d}Z", ended_at=f"2026-10-18T00:00:{end:02d}Z")

    result = time_partition(
        [call("model", 0, 10), call("model", 5, 8), call("tool", 7, 12), call("model", 15, 20), dict(kind="model", started_at=None, ended_at=None)]
    )
    assert {k: result[k] for k in ("model_only_ms", "tool_only_ms", "overlap_ms", "gap_ms", "wall_span_ms")} == dict(
        model_only_ms=12000, tool_only_ms=2000, overlap_ms=3000, gap_ms=3000, wall_span_ms=20000
    )
    assert result["missing_intervals"] == 1
    assert time_partition([])["wall_span_ms"] is None


def test_projected_report_with_frozen_stream_metrics_is_json_serializable():
    store, bundle = sample()
    encoded = canonical(bundle)
    restored = json.loads(encoded)
    for call in restored["execution_view"]["calls"]:
        if call["kind"] == "model":
            assert call["stream_metrics"] == {"http_receive_ms": 9000, "delta_emit_ms": 20}
    # Projection must not mutate the immutable trace payload.
    assert execution_view(store, bundle) == restored["execution_view"]


def test_projection_expands_window_without_reassigning_calls_or_cost():
    store, bundle = sample()
    ledger = deepcopy(bundle["cost_ledger"])
    result = execution_view(store, bundle)
    assert result["run"]["timing"]["wall_span_ms"] == 41000
    assert len(result["phases"]["p"]["call_ids"]) == 4
    assert len(result["phases"]["p"]["related_call_ids"]) == 1
    assert len(result["phases"]["p"]["other_call_ids"]) == 3
    assert len(result["run"]["unassigned_call_ids"]) == 5
    assert len(result["run"]["outside_phase_call_ids"]) == 2
    assert result["run"]["costs"]["total_tokens"] == ledger["totals"]["total_tokens"]
    assert bundle["cost_ledger"] == ledger
    assert result["phases"]["p"]["timing"]["wall_span_ms"] == 22000
    assert result["phases"]["p"]["related_timing"]["wall_span_ms"] == 10000


def test_round_graph_uses_exact_invocation_ids_and_public_source_excerpts():
    store, bundle = sample()
    result = execution_view(store, bundle)
    assert len(result["rounds"]) == 3
    assert all(len(r["tool_call_ids"]) == 1 for r in result["rounds"])
    assert sum(e["kind"] == "invokes" for e in result["edges"]) == 3
    assert sum(e["kind"] == "returns" for e in result["edges"]) == 3
    assert sum(e["kind"] == "source_order" for e in result["edges"]) == 2
    assert "hidden must not leak" not in str(result)
    for call in result["calls"]:
        if call["kind"] == "model":
            assert store.resolve(call["statement_refs"][0]) == call["statement"]
            assert call["stream_metrics"] == {"http_receive_ms": 9000, "delta_emit_ms": 20}
    assert all(e["basis"] == "archive_order_not_causal_adoption" for e in result["edges"] if e["kind"] == "source_order")


def test_missing_tool_invocation_ids_are_not_reconstructed_from_proximity():
    events = action(1)
    for event in events[2:]:
        event.pop("llm_call_id")
    store = source(events)
    facts = build_behavior_facts(store)
    bundle = project(store, facts, "run", {})
    assert len(bundle["execution_view"]["rounds"]) == 2
    assert not any(e["kind"] == "invokes" for e in bundle["execution_view"]["edges"])
    assert bundle["execution_view"]["run"]["timing"]["missing_intervals"] == 2


def test_reused_tool_ids_in_other_traces_do_not_link_to_wrong_model():
    events = action(1) + action(2)
    for i in (0, 4):
        events[i + 1]["response"]["tool_calls"] = [{"id": "same", "function": {"name": "read_file", "arguments": "{}"}}]
        events[i + 2]["tool_call_id"] = events[i + 3]["tool_call_id"] = "same"
    store = source(events)
    facts = build_behavior_facts(store)
    result = project(store, facts, "run", {})["execution_view"]
    assert len(result["rounds"]) == 2
    assert all(len(r["tool_call_ids"]) == 1 for r in result["rounds"])
    assert len({r["tool_call_ids"][0] for r in result["rounds"]}) == 2


def test_managed_json_finish_links_report_generation_without_native_tool_calls():
    events = action(1, tool='finish', inp={'report': 'Final report'})
    for event in events[:2]:
        event['llm_call_id'] = 'trace1-llm-7'
    events[1]['response']['content'] = json.dumps({'action': {'kind': 'tool', 'target': 'finish', 'input': {'report': 'Final report'}}})
    for event in events[2:]:
        event.pop('llm_call_id')
        event['tool_call_id'] = 'trace1-json-0-7'
    store = source(events)
    bundle = project(store, build_behavior_facts(store), 'run', {})
    flow = bundle['execution_view']['action_flow']
    assert [r['kind'] for r in flow['actions']] == ['final_answer', 'final_answer']
    assert flow['actions'][0]['title'] == 'Working · Preparing finish'
    # Two occurrences of the same model ID are ambiguous, even with the same JSON action.
    events = events[:2] + deepcopy(events[:2]) + events[2:]
    store = source(events)
    view = project(store, build_behavior_facts(store), 'run', {})['execution_view']
    assert not any(e['kind'] == 'invokes' for e in view['edges'])
