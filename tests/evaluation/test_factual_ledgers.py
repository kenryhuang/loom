"""Factual analysis must preserve evidence without promoting it to task success."""

import json
from dataclasses import asdict


def event(kind, *, call="c1", step=0, trace="t", **payload):
    if isinstance(payload.get("output"), dict) and set(payload["output"]) == {"value"}:
        payload["output"] = {"id": "observation", "source": payload.get("tool_id"), "at": "2026-09-05T00:00:00Z",
                             **payload["output"]}
    return {"type": kind, "run_id": "r", "loop_id": "l", "trace_id": trace,
            "step_number": step, "llm_call_id": call, **payload}


def facts_for(tmp_path, rows, task=None):
    from loom.evaluation.evidence_store import EvidenceStore
    from loom.evaluation.trajectory import build_fact_analysis
    from loom.trace_analysis.graph import build_episode_graph

    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    store = EvidenceStore.open(path)
    return build_fact_analysis(store, build_episode_graph(store.events), task), store


def test_context_tracks_removal_retention_repetition_and_schema_change_across_steps(tmp_path):
    system = {"role": "system", "content": "Goal: find evidence"}
    prior = {"role": "user", "content": "old evidence"}
    newer = {"role": "user", "content": "new evidence"}
    facts, store = facts_for(tmp_path, [
        event("llm.requested", messages=[system, prior], tools=[{"name": "read", "version": 1}]),
        event("llm.completed", response={"content": "continue"}),
        event("llm.requested", call="c2", step=4, trace="t2", messages=[system, newer, newer],
              tools=[{"name": "read", "version": 2}]),
    ])
    delta = facts.context_deltas[1]
    assert delta["previous_round_id"] == facts.trajectory[0]["id"]
    assert [u["excerpt"] for u in delta["removed"]] == ["old evidence"]
    assert [u["excerpt"] for u in delta["retained"]] == ["Goal: find evidence"]
    assert len(delta["added"]) == 2
    assert delta["repeated_unit_count"] == 1
    assert len(delta["tool_schemas"]["added"]) == len(delta["tool_schemas"]["removed"]) == 1
    assert delta["waste_status"] == "not_assessed"
    assert store.resolve(delta["removed"][0]["ref"]) == prior
    json.dumps(asdict(facts))


def test_tool_negative_information_and_cross_step_injection_are_observed_not_semantic_use(tmp_path):
    raw = {"matches": [], "content": "No matching declaration in recorded file", "truncated": False}
    rows = [
        event("llm.requested", messages=[{"role": "user", "content": "Find declaration"}]),
        event("llm.completed", response={"content": "search"}),
        event("tool.started", tool_call_id="c1-json-tool-1", tool_id="search", input={"query": "name"}),
        event("tool.completed", tool_call_id="c1-json-tool-1", tool_id="search", output={"value": raw}),
        event("llm.requested", call="c2", step=3, trace="next", messages=[
            {"role": "tool", "tool_call_id": "c1-json-tool-1", "content": json.dumps(raw)}]),
    ]
    facts, store = facts_for(tmp_path, rows)
    use = facts.tool_uses[0]
    assert use["status"] == "complete"
    assert store.resolve(use["raw_output_ref"]) == raw
    assert use["injections"][0]["round_id"] == facts.trajectory[1]["id"]
    assert use["semantic_consumption"] == "unknown"
    assert use["task_effect"] == "not_assessed"
    assert not any(v["execution_status"] == "failed" for v in facts.verification_evidence)


def test_future_tool_output_is_never_attached_to_past_context(tmp_path):
    facts, _ = facts_for(tmp_path, [
        event("llm.requested", messages=[{"role": "tool", "tool_call_id": "tool", "content": "later"}]),
        event("llm.completed", response={"content": "search"}),
        event("tool.completed", tool_call_id="tool", tool_id="read", output="later"),
    ])
    assert facts.tool_uses[0]["injections"] == []


def test_usage_missing_conflicting_duplicate_and_auxiliary_are_not_complete(tmp_path):
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    first = event("llm.completed", response={"usage": usage})
    facts, _ = facts_for(tmp_path, [
        event("llm.requested", messages=[]), first, first,
        event("llm.requested", call="c2", messages=[]),
        event("llm.completed", call="c2", response={}),
        event("llm.completed", call="c3", response={"usage": usage}),
        event("llm.completed", call="c3", response={"usage": {**usage, "prompt_tokens": 11, "total_tokens": 13}}),
        event("llm.completed", call="aux", step=None, trace=None, response={"usage": usage}),
    ])
    ledger = {r["llm_call_id"]: r for r in facts.token_ledger}
    assert ledger["c1"]["usage_status"] == "duplicate"
    assert ledger["c1"]["total_tokens"] == 12
    assert ledger["c2"]["total_tokens"] is None
    assert ledger["c2"]["usage_status"] == "unknown"
    assert ledger["c3"]["usage_status"] == "conflicting"
    assert ledger["c3"]["total_tokens"] is None
    assert facts.coverage["tokens"]["status"] != "complete"
    assert facts.coverage["tokens"]["auxiliary_usage"]


def test_usage_totals_are_provider_fields_and_not_character_estimates(tmp_path):
    facts, _ = facts_for(tmp_path, [
        event("llm.requested", messages=[{"role": "user", "content": "x" * 50000}]),
        event("llm.completed", response={"usage": {"promptTokens": 30, "completionTokens": 4, "totalTokens": 34}}),
    ])
    assert facts.token_ledger[0]["total_tokens"] == 34
    assert facts.coverage["tokens"]["prompt_tokens"] == 30
    assert facts.coverage["tokens"]["status"] == "complete"


def test_reused_call_ids_account_each_completed_occurrence_once(tmp_path):
    first = event("llm.completed", response={"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    facts, _ = facts_for(tmp_path, [
        event("llm.requested", messages=[]), first, first,
        event("llm.requested", messages=[]),
        event("llm.completed", response={"usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}}),
    ])
    assert len(facts.token_ledger) == 2
    assert [row["total_tokens"] for row in facts.token_ledger] == [12, 23]
    assert facts.coverage["tokens"]["known_total_tokens"] == 35


def test_task_contract_parses_full_initial_system_goal_and_later_revision(tmp_path):
    initial = ("Instructions\n" + "background " * 500
               + "\nGoal:\n- Objective: construct and run smoke\nSuccess criteria:\n"
               "- criterion-1 (required): report results\n- criterion-2: run smoke\n\nAvailable tools:\n- finish: submit report")
    revised = initial.replace("construct and run smoke", "reuse and run smoke")
    facts, store = facts_for(tmp_path, [
        event("llm.requested", messages=[{"role": "system", "content": initial}, {"role": "user", "content": "Current state"}]),
        event("llm.requested", call="c2", messages=[{"role": "system", "content": revised}]),
    ], task="Audit smoke coverage")
    explicit, first, revision = facts.task_contracts
    assert explicit["origin"] == "explicit_task"
    assert explicit["evidence_refs"] == []
    assert first["objective"] == "construct and run smoke"
    assert [c["description"] for c in first["criteria"]] == ["report results", "run smoke", "construct and run smoke"]
    assert revision["objective"] == "reuse and run smoke"
    assert revision["supersedes"] == first["id"]
    assert len({c["id"] for t in facts.task_contracts for c in t["criteria"]}) == 7
    assert first["criteria"][-1]["origin"] == "objective"
    assert explicit["criteria"][-1]["description"] == explicit["objective"]
    assert store.resolve(first["evidence_refs"][0]) == initial


def test_stdout_pass_runtime_budget_and_file_assertions_are_distinct(tmp_path):
    rows = [event("llm.requested", messages=[{"role": "user", "content": "Verify search"}])]
    rows += [event("tool.started", tool_call_id="read", tool_id="read_file", input={"path": "smoke.py"}),
             event("tool.completed", tool_call_id="read", tool_id="read_file", output={"value": {
                 "path": "smoke.py", "content": 'assert result["count"] > 0\nprint("PASSED")\n', "truncated": False}}),
             event("tool.started", tool_call_id="shell", tool_id="shell_execute", input={"command": "python smoke.py"}),
             event("tool.completed", tool_call_id="shell", tool_id="shell_execute", output={"value": {
                 "stdout": "PASSED\n1 passed, 2 warnings\nRuntimeWarning: coroutine was never awaited", "stderr": "", "exit_code": 0}}),
             event("tool.completed", tool_call_id="write", tool_id="write_file", input={"path": "smoke.py", "content": "changed"},
                   output={"value": {"written": True}}),
             event("run.completed", outcome="pass", metadata={"stop_reason": "max_steps"})]
    facts, store = facts_for(tmp_path, rows)
    records = facts.verification_evidence
    check = next(r for r in records if r["kind"] == "command_execution")
    assert check["execution_status"] == "passed"
    assert check["criterion_status"] == "unverified"
    assert check["warnings"]
    assert check["freshness"]["later_recorded_writes"]
    source = next(r for r in records if r["kind"] == "recorded_assertions")
    assert source["assertions"][0]["text"] == 'assert result["count"] > 0'
    assert source["execution_status"] == "unknown"
    assert source["assertions"][0]["text"] in store.resolve(source["assertions"][0]["ref"])
    runtime = next(r for r in records if r["kind"] == "runtime_outcome")
    assert runtime["execution_status"] == "unknown"
    assert runtime["criterion_status"] == "unverified"


def test_printed_pass_without_exit_code_and_timed_out_zero_are_not_success(tmp_path):
    facts, _ = facts_for(tmp_path, [
        event("tool.completed", tool_call_id="a", tool_id="shell_execute", output={"stdout": "PASSED"}),
        event("tool.completed", tool_call_id="b", tool_id="shell_execute", output={"exit_code": 0, "timed_out": True}),
        event("tool.completed", tool_call_id="c", tool_id="shell_execute", output={"exit_code": 1, "stdout": "No matches"}),
    ])
    statuses = [r["execution_status"] for r in facts.verification_evidence if r["kind"] == "command_execution"]
    assert statuses == ["unknown", "failed", "failed"]


def test_legacy_transcript_keeps_raw_result_and_reports_omitted_fields(tmp_path):
    injected = {"tool": "shell_execute", "input": {"command": "pytest"},
                "result": {"stdout": "1 passed", "exit_code": 0}}
    facts, _ = facts_for(tmp_path, [
        event("llm.requested", messages=[]),
        event("tool.started", tool_call_id="x", tool_id="shell_execute", input={"command": "pytest"}),
        event("tool.completed", tool_call_id="x", tool_id="shell_execute",
              output={"value": {**injected["result"], "duration_ms": 10}}),
        event("llm.requested", call="c2", messages=[{"role": "assistant", "content": "Tool execution transcript:\n" + json.dumps([injected])}]),
    ])
    injection = facts.tool_uses[0]["injections"][0]
    assert injection["raw_output_preserved"] is False
    assert injection["omitted_output_fields"] == ["duration_ms"]
    assert injection["basis"] == "recorded_tool_transcript"


def test_retained_prior_goal_does_not_revert_task_revision(tmp_path):
    old = {"role": "system", "content": "Goal: build test\nSuccess criteria:\n- it runs"}
    revised = {"role": "user", "content": "Objective: reuse test\nSuccess criteria:\n- run existing test"}
    facts, _ = facts_for(tmp_path, [
        event("llm.requested", messages=[old]),
        event("llm.requested", call="c2", messages=[old, revised]),
        event("llm.requested", call="c3", messages=[old, revised]),
    ])
    assert [t["objective"] for t in facts.task_contracts] == ["build test", "reuse test"]


def test_unidentified_auxiliary_call_without_usage_makes_total_incomplete(tmp_path):
    facts, _ = facts_for(tmp_path, [
        event("llm.requested", messages=[]),
        event("llm.completed", response={"usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}),
        event("llm.failed", call=None, step=None, trace=None, error="auxiliary failed before reporting usage"),
    ])
    assert facts.coverage["tokens"]["status"] == "incomplete"
    assert facts.coverage["tokens"]["total_tokens"] is None
    assert facts.coverage["tokens"]["known_total_tokens"] == 6


def test_task_update_records_and_written_assertion_contents_keep_provenance(tmp_path):
    facts, store = facts_for(tmp_path, [
        event("task.created", objective="create test", criteria=[{"id": "a", "description": "test exists"}]),
        event("task.updated", objective="extend test", criteria=[{"id": "a", "description": "test checks count"}]),
        event("tool.completed", tool_call_id="w", tool_id="write_file", input={"path": "test.py", "content": "assert count == 2\n"},
              output={"value": {"written": True}}),
    ])
    assert [c["objective"] for c in facts.task_contracts] == ["create test", "extend test"]
    assert facts.task_contracts[1]["supersedes"] == facts.task_contracts[0]["id"]
    assertion = next(r for r in facts.verification_evidence if r["kind"] == "recorded_assertions")
    assert assertion["execution_status"] == "unknown"
    assert store.resolve(assertion["content_ref"]) == "assert count == 2\n"
