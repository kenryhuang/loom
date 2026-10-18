from loom.evaluation.goal_loops.action_flow import action_flow, classify


def call(id, kind, start, end, **extra):
    return dict(
        id=id,
        kind=kind,
        status="success",
        started_at=f"2026-10-18T00:00:{start:02d}Z" if start is not None else None,
        ended_at=f"2026-10-18T00:00:{end:02d}Z" if end is not None else None,
        duration_ms=None,
        start_line=start or 0,
        **extra,
    )


def round_(id, line, tools=()):
    return dict(id=id, model_call_id=id, start_line=line, tool_call_ids=list(tools))


def test_action_types_follow_recorded_operations_not_statement_keywords():
    for extra, expected in [
        ({"tool": "read_file"}, "research"),
        ({"tool": "edit_file"}, "implementation"),
        ({"tool": "shell_execute", "target": "npx tsc --noEmit"}, "verification"),
        ({"tool": "shell_execute", "target": "find . -type f | head -100"}, "research"),
        ({"tool": "shell_execute", "target": "find . -delete"}, "other"),
        ({"tool": "shell_execute", "target": "find . | xargs rm"}, "other"),
        ({"tool": "ext_search", "target_key": "query"}, "research"),
        ({"tool": "ext_speech", "target_key": "text"}, "other"),
        ({"tool": "finish"}, "final_answer"),
        ({"tool": "complete_node"}, "verification"),
    ]:
        assert classify({"kind": "tool", **extra})[0] == expected
    assert classify({"kind": "model", "stage": "plan", "role": "verification"})[0] == "planning"
    assert classify({"kind": "model", "statement": "I will plan, research, verify and finish"})[0] == "other"


def test_consecutive_actions_group_but_return_to_research_stays_separate():
    calls = []
    rounds = []
    for i, tool in enumerate(["read_file", "read_file", "edit_file", "read_file"]):
        model = f"m{i}"
        tid = f"t{i}"
        calls.extend([call(model, "model", i * 10, i * 10 + 5), call(tid, "tool", i * 10 + 5, i * 10 + 7, tool=tool, target="a.txt")])
        rounds.append(round_(model, i * 4, [tid]))
    result = action_flow(calls, rounds)
    assert [s["kind"] for s in result["segments"]] == ["research", "implementation", "research"]
    assert [len(s["action_ids"]) for s in result["segments"]] == [4, 2, 2]
    assert [s["elapsed_ms"] for s in result["segments"]] == [14000, 7000, 7000]
    assert result["gap_ms"] == 9000 and result["wall_span_ms"] == 37000
    assert len({c for a in result["actions"] for c in a["call_ids"]}) == len(calls)


def test_nested_acceptance_is_counted_once_and_parallel_models_are_shared():
    calls = [call("m", "model", 0, 10), call("finish", "tool", 10, 30, tool="finish"), call("judge", "model", 15, 25, role="verification", stage="verify")]
    result = action_flow(calls, [round_("m", 1, ["finish"]), round_("judge", 4)])
    assert [s["elapsed_ms"] for s in result["segments"]] == [20000, 10000]
    assert [a["elapsed_ms"] for a in result["actions"]] == [10000, 10000, 10000]
    assert result["segments"][0]["model_ms"] == result["segments"][0]["tool_ms"] == 10000
    assert result["segments"][-1]["label"] == "Acceptance verification"
    assert result["parallel_ms"] == 0 and result["wall_span_ms"] == 30000
    calls = [call("one", "model", 0, 10), call("two", "model", 5, 15)]
    result = action_flow(calls, [round_("one", 1), round_("two", 2)])
    assert result["parallel_ms"] == 5000
    assert result["segments"][0]["elapsed_ms"] == 10000
    assert result["segments"][0]["shared_ms"] == 5000  # Same-stage overlap counted once.


def test_non_nested_parallel_tool_is_not_subtracted_as_acceptance():
    calls = [call("one", "model", 0, 10), call("t", "tool", 5, 15, tool="read_file")]
    result = action_flow(calls, [round_("one", 1), dict(id="t", model_call_id=None, start_line=2, tool_call_ids=["t"])])
    assert result["parallel_ms"] == 5000 and sum(s["elapsed_ms"] for s in result["segments"]) == 10000


def test_mixed_and_unmeasured_actions_remain_explicit():
    calls = [call("m", "model", None, None), call("read", "tool", None, None, tool="read_file"), call("write", "tool", None, None, tool="edit_file")]
    result = action_flow(calls, [round_("m", 1, ["read", "write"])])
    assert result["segments"][0]["kind"] == "mixed"
    assert result["missing_intervals"] == 3 and result["wall_span_ms"] is None
    assert [a["activity"] for a in result["actions"]] == ["model", "tool", "tool"]


def test_working_time_and_tool_execution_are_separate_with_explicit_gaps():
    calls = [call("model", "model", 0, 30), call("edit", "tool", 32, 32, tool="edit_file")]
    calls[0].update(duration_ms=30123.5, ttft_ms=123.5, model="test")
    calls[1].update(duration_ms=0, duration_basis="timestamps")
    result = action_flow(calls, [round_("model", 0, ["edit"])])
    working, tool = result["actions"]
    assert working["title"] == "Working · Preparing edit_file"
    assert working["duration_ms"] == 30123.5
    assert working["elapsed_ms"] == 30000
    assert tool["title"] == "edit_file" and tool["duration_ms"] == 0
    assert working["call_ids"] == ["model"] and tool["call_ids"] == ["edit"]
    stage = result["segments"][0]
    assert (stage["model_ms"], stage["tool_ms"]) == (30000, 0)
    assert (stage["model_calls"], stage["tool_calls"]) == (1, 1)
    assert result["gaps"][0]["duration_ms"] == 2000
    assert result["gaps"][0]["next_action_ids"] == ["edit"]
    assert sum(g["duration_ms"] for g in result["gaps"]) == result["gap_ms"]


def test_failed_or_unlinked_model_call_remains_visible():
    model = call("m", "model", 0, None)
    model.update(status="failed", duration_ms=1234)
    result = action_flow([model], [round_("m", 1)])
    assert len(result["actions"]) == 1
    assert result["actions"][0]["title"] == "Working · Model response"
    assert result["actions"][0]["status"] == "failed"
    assert result["actions"][0]["duration_ms"] == 1234
    assert result["missing_intervals"] == 1
