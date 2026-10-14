import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from loom.core import ok
from loom.llm import LlmResponse, LlmToolCall
from loom.llm.context_budget import estimate_message_tokens
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from tests.tasks.test_acceptance import Judge, Sink

TOOL = "ext_web_search"
CRITERION = {"id": "web-search", "description": "Use WebSearch MCP for planet research", "verifier": "tool_call",
             "scope": [], "check": {"tool_id": TOOL}}


@pytest.fixture
def request_and_search(tmp_path, monkeypatch):
    from loom.external_tools import tools

    binding = {"id": TOOL, "connector_id": "web", "connector_name": "Bailian", "name": "WebSearch",
               "description": "Search the web", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
               "effect_kind": "read_only", "kind": "mcp"}
    response = {"content": [{"type": "text", "text": json.dumps({"pages": [{"title": "Neptune", "url": "https://example.org/neptune"}]})}],
                "isError": False}

    async def call(config, name, value):
        return response, response.get("isError", False)

    monkeypatch.setattr(tools, "call_mcp", call)
    monkeypatch.setattr(tools.ExternalToolStore, "connection_for", lambda *args: {"kind": "mcp", "config": {}})
    (tmp_path / "app.txt").write_text("planet descriptions")
    return TaskRequest("Use WebSearch MCP to research planets", workspace=tmp_path,
                       task_spec={"session_environment": {"plugin": "session", "resources": [{"id": "workspace", "kind": "directory", "uri": str(tmp_path),
                                                                         "access": "read-write"}]},
                                  "tools": {"collections": ["filesystem", "external_tools", "task_control"]}},
                       metadata={"external_tool_bindings": [binding], "external_tools_directory": str(tmp_path / "registry")}), response


def test_mcp_evidence_survives_later_reads_restore_and_goal_changes(request_and_search):
    request, response = request_and_search

    async def exercise():
        assembly = TaskAssembly(request, plan_mode="off")
        context = make_task_context(request, assembly=assembly, plan_mode="off").unwrap()
        assembly.acceptance.goal(context)
        assembly.acceptance.data["plan"] = assembly.acceptance.validate_plan({"criteria": [CRITERION]})
        handlers = assembly.handlers()
        search_options = {"metadata": {"tool_call_id": "original-search"}}
        assert (await handlers[TOOL]({"query": "Neptune"}, search_options)).ok
        for _ in range(12):
            assert (await handlers["read_file"]({"path": "app.txt"})).ok
        # Replay of the same journal invocation must not create an extra success.
        assert (await handlers[TOOL]({"query": "Neptune"}, search_options)).ok
        row, detail = await assembly.acceptance.check(CRITERION, None)
        assert row["status"] == "passed" and detail["successful"] == 1
        # Move the search out of the recent tail again, then restore its durable index.
        for _ in range(10):
            await handlers["read_file"]({"path": "app.txt"})
        snapshot = assembly.snapshot()
        restored = TaskAssembly(request, plan_mode="off")
        restored._local_artifacts = assembly._local_artifacts
        restored.restore(snapshot)
        evidence = restored.acceptance_evidence()
        search = next(r for r in evidence if r.get("tool") == TOOL)
        assert "Neptune" in search["output"] and "example.org/neptune" in search["output"]
        assert search["ok"] and not search["truncated"]
        assert (await restored.acceptance.check(CRITERION, None))[0]["status"] == "passed"
        restored.acceptance.goal(replace(context, goal=replace(context.goal, objective="A different task")))
        assert (await restored.acceptance.check(CRITERION, None))[0]["status"] == "failed"
        await assembly.close()
        await restored.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("application_error", [True, False])
def test_execution_success_is_separate_from_useful_results(request_and_search, application_error):
    request, response = request_and_search
    response.update(isError=application_error, content=[{"type": "text", "text": '{"status":1,"pages":[],"tools":[]}'}])

    async def exercise():
        assembly = TaskAssembly(request, plan_mode="off")
        assembly.acceptance.goal(make_task_context(request, assembly=assembly, plan_mode="off").unwrap())
        await assembly.handlers()[TOOL]({"query": "Neptune"})
        result, detail = await assembly.acceptance.check(CRITERION, None)
        assert result["status"] == ("failed" if application_error else "passed")
        # Empty pages are visible to the semantic judge even when the transport succeeded.
        evidence = next(r for r in assembly.acceptance_evidence() if r.get("tool") == TOOL)
        assert evidence["ok"] is not application_error
        assert "goal completion require semantic" in detail["assurance"]
        if not application_error:
            assert '"pages": []' in evidence["output"] or '\\"pages\\":[]' in evidence["output"]
        await assembly.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("useful_results", [True, False])
def test_plan_correction_then_mcp_execution_reaches_semantic_acceptance(request_and_search, useful_results):
    request, response = request_and_search
    if not useful_results:
        response["content"] = [{"type": "text", "text": '{"pages":[]}'}]

    class ResearchJudge(Judge):
        def __init__(self):
            super().__init__([CRITERION, {"id": "sources", "description": "Relevant planet sources support the answer",
                                         "verifier": "semantic", "scope": [], "check": {}}])
            self.passed = useful_results
            self.proposals = 0
            self.solver_calls = 0
            self.reviewed = False

        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            prompt = messages[0].content
            if prompt.startswith("You design Loom task acceptance"):
                self.proposals += 1
                if self.proposals == 1:
                    return ok(LlmResponse(content=json.dumps({"criteria": [{**CRITERION, "verifier": "external",
                                                                           "check": {"reason": "Verify MCP was used"}}]})))
                assert "No external approval feedback channel" in messages[1].content
            elif prompt.startswith("You independently verify"):
                payload = json.loads(messages[1].content)
                assert {c["id"] for c in payload["criteria"]} == {"sources", "goal"}
                search = next(r for r in payload["evidence"] if r.get("tool") == TOOL)
                assert search["ok"]
                assert ("Neptune" in search["output"]) is useful_results
                self.reviewed = True
            else:
                self.solver_calls += 1
                if self.solver_calls == 1:
                    return ok(LlmResponse(tool_calls=(LlmToolCall("search", TOOL, '{"query":"Neptune"}'),)))
                if self.solver_calls == 2:
                    return ok(LlmResponse(tool_calls=tuple(LlmToolCall(f"read-{i}", "read_file", '{"path":"app.txt"}') for i in range(12))))
                return ok(LlmResponse(tool_calls=(LlmToolCall("done", "finish", '{"report":"Neptune: source https://example.org/neptune"}'),)))
            return await super().chat(messages, tools, cancellation, **kwargs)

    judge, sink = ResearchJudge(), Sink()
    result = asyncio.run(run_generic_task(request, provider=judge, trace_sink=sink,
                                         options=TaskRunOptions(plan_mode="off", max_steps=8))).unwrap()
    acceptance = result.run_result.context.state.scratch["acceptance"]
    assert acceptance["state"] == ("passed" if useful_results else "blocked"), acceptance
    assert next(r for r in acceptance["results"] if r["criterion_id"] == "web-search")["status"] == "passed"
    assert judge.reviewed and judge.proposals == 2
    assert any(e["type"] == "acceptance.plan.rejected" for e in sink.events)
    assert any(e["type"] == "run.completed" for e in sink.events) is useful_results


def test_legacy_external_plan_requires_replanning_not_automatic_pass(tmp_path):
    assembly = TaskAssembly(TaskRequest("Use MCP", workspace=tmp_path), plan_mode="off")
    snapshot = assembly.acceptance.snapshot()
    old = {"criteria": [{**CRITERION, "verifier": "external"}], "unresolved_requirements": []}
    snapshot.update(plan=old, state="blocked", attempts=3, model_calls=6)
    assembly.acceptance.restore(snapshot)
    assert assembly.acceptance.data["plan"] is None
    assert assembly.acceptance.data["history"] == [old]
    assert assembly.acceptance.data["state"] == "not_ready"
    assert assembly.acceptance.data["attempts"] == 0


def test_large_evidence_is_sampled_with_explicit_coverage_within_context(request_and_search):
    request, response = request_and_search
    response["content"] = [{"type": "text", "text": "海王星资料" * 4000}]

    async def exercise():
        assembly = TaskAssembly(request, plan_mode="off")
        assembly.acceptance_provider = SimpleNamespace(model="small", context_window_tokens=8000)
        assembly.acceptance.goal(make_task_context(request, assembly=assembly, plan_mode="off").unwrap())
        assembly.acceptance.data["plan"] = assembly.acceptance.validate_plan({"criteria": [CRITERION]})
        for i in range(20):
            await assembly.handlers()[TOOL]({"query": f"planet {i}"})
        evidence = assembly.acceptance_evidence()
        assert estimate_message_tokens("", json.dumps(evidence, ensure_ascii=False)) <= 2400
        assert evidence[0]["total_calls"] == 20
        assert evidence[0]["omitted_calls"] == 20 - evidence[0]["reviewed_calls"]
        assert evidence[0]["omitted_calls"] > 0
        assert any(r.get("truncated") for r in evidence[1:])
        assert (await assembly.acceptance.check(CRITERION, None))[1]["successful"] == 20
        # Prompt clipping never destroys the original output.
        ref = assembly._acceptance_calls[0]["artifact"]
        assert "海王星资料" * 4000 in json.dumps(assembly.read_artifact(ref["sha256"]), ensure_ascii=False)
        await assembly.close()

    asyncio.run(exercise())


def test_new_evidence_artifact_is_reused_on_journal_replay(tmp_path):
    class Bridge:
        saved = None
        artifacts = 0

        def operation_start(self, call):
            return self.saved

        def operation_finish(self, call, result):
            self.saved = result

        def rpc(self, kind, value):
            assert kind == "publish_artifact"
            self.artifacts += 1
            return {"sha256": "digest", "kind": value["kind"]}

    async def exercise():
        (tmp_path / "file.txt").write_text("original")
        request = TaskRequest("Read", workspace=tmp_path)
        bridge = Bridge()
        first = TaskAssembly(request, execution=bridge, run_id="run", plan_mode="off")
        before = first.snapshot()
        options = {"metadata": {"tool_call_id": "read"}}
        original = (await first.handlers()["read_file"]({"path": "file.txt"}, options)).unwrap()
        restored = TaskAssembly(request, execution=bridge, run_id="run", plan_mode="off")
        restored.restore(before)
        (tmp_path / "file.txt").write_text("modified")
        replayed = (await restored.handlers()["read_file"]({"path": "file.txt"}, options)).unwrap()
        assert original == replayed and bridge.artifacts == 1
        assert len(restored._acceptance_calls) == 1
        await first.close()
        await restored.close()

    asyncio.run(exercise())


def test_nonzero_exit_and_verification_calls_do_not_satisfy_tool_use(tmp_path):
    async def exercise():
        assembly = TaskAssembly(TaskRequest("Run a successful check", workspace=tmp_path), plan_mode="off")
        assembly.acceptance.goal(make_task_context(assembly.request, assembly=assembly, plan_mode="off").unwrap())
        criterion = {**CRITERION, "check": {"tool_id": "process_execute"}}
        execute = assembly.handlers()["process_execute"]
        await execute({"argv": [sys.executable, "-c", "raise SystemExit(7)"]})
        assert (await assembly.acceptance.check(criterion, None))[0]["status"] == "failed"
        assembly.acceptance.verifying = True
        await execute({"argv": [sys.executable, "-c", "pass"]})
        assembly.acceptance.verifying = False
        assert (await assembly.acceptance.check(criterion, None))[1]["successful"] == 0
        await execute({"argv": [sys.executable, "-c", "pass"]})
        assert (await assembly.acceptance.check(criterion, None))[1]["successful"] == 1
        await assembly.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("check", [{"tool_id": "invented"}, {"tool_id": "read_file", "min_calls": True},
                                   {"tool_id": "read_file", "min_calls": 0}, {"tool_id": "read_file", "success": True}])
def test_invalid_tool_check_is_rejected(tmp_path, check):
    assembly = TaskAssembly(TaskRequest("Read", workspace=tmp_path), plan_mode="off")
    with pytest.raises(ValueError, match="Tool-call"):
        assembly.acceptance.validate_plan({"criteria": [{**CRITERION, "check": check}]})
