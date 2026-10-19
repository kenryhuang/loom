import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from loom.core import Observation, now_iso, ok
from loom.llm import LlmResponse, TokenUsage
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from loom.tasks.workspace_probe import ManifestUnavailable
from tests.tasks.test_acceptance import Judge, Sink


def command(scope=()):
    return {"id": "condition", "description": "Observed state satisfies the required condition", "verifier": "command",
            "scope": list(scope), "check": {"tool_id": "shell_execute", "input": {"command": "printf observed"}}}


@pytest.mark.parametrize("scope", [[], ["."]])
def test_uncacheable_verification_runs_fresh_after_restore_and_keeps_host_completion_local(tmp_path, monkeypatch, scope):
    if scope:
        def oversized(*args, **kwargs):
            raise ManifestUnavailable("Verification scope exceeds 2048 entries")
        monkeypatch.setattr("loom.tasks.acceptance.manifest", oversized)
    assembly = TaskAssembly(TaskRequest("Check state", workspace=tmp_path), plan_mode="off")
    judge, sink, calls = Judge([command(scope)]), Sink(), []
    assembly.configure_acceptance(judge)
    context = make_task_context(assembly.request, assembly=assembly, plan_mode="off").unwrap()

    async def call_tool(tool, value, metadata):
        calls.append(metadata["tool_call_id"])
        return ok(Observation("observed", tool, {"exit_code": 0, "stdout": "observed"}, now_iso()))

    runtime = SimpleNamespace(run_id="run", loop_id="loop", trace_id="trace", trace_sink=sink, call_tool=call_tool, cancellation=None)
    controller = assembly.acceptance

    async def scenario():
        assert await controller.verify(context, runtime, "answer")
        assert not controller.applicable("answer")
        assert controller.data["cache"]["reusable"] is False
        assert await controller.verify(context, runtime, "answer", reuse_current=True)
        assert len(calls) == 1  # Host finalization consumes the current result without rerunning it.
        controller.restore(controller.snapshot())
        assert await controller.verify(context, runtime, "answer", reuse_current=True)
        assert len(calls) == len(set(calls)) == 2  # Neither checkpoint nor journal reuses old success.
        assert await controller.verify(context, runtime, "answer")
        assert len(calls) == len(set(calls)) == 3
        assert all(c["verifier"] == "command" for c in controller.data["plan"]["criteria"] if c["id"] == "condition")

    asyncio.run(scenario())


def test_broad_tree_with_secret_is_not_read_or_required_for_command_verification(tmp_path):
    (tmp_path / ".env").write_text("PRIVATE_SENTINEL")
    judge, sink = Judge([command(["."])], finish=True), Sink()
    result = asyncio.run(run_generic_task(
        TaskRequest("Check state", workspace=tmp_path), provider=judge,
        options=TaskRunOptions(plan_mode="off", max_steps=3), trace_sink=sink,
    )).unwrap()
    acceptance = result.run_result.context.state.scratch["acceptance"]
    assert acceptance["state"] == "passed"
    assert acceptance["cache"]["reusable"] is False
    assert "PRIVATE_SENTINEL" not in " ".join(m.content for call in judge.calls for m in call)
    assert len([e for e in sink.events if e["type"] == "tool.started" and e.get("tool_id") == "shell_execute"]) == 1
    assert len([e for e in sink.events if e["type"] == "verification.completed" and e["result"]["criterion_id"] == "condition"]) == 1
    assembly = TaskAssembly(TaskRequest("Check", workspace=tmp_path), plan_mode="off")
    for path in (".env", "../outside"):
        with pytest.raises(ValueError):
            assembly.acceptance.validate_plan({"criteria": [command([path])]})


def test_zero_exit_code_is_reviewed_against_condition_in_same_judgment_call(tmp_path):
    class OutcomeJudge(Judge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if messages[0].content.startswith("You independently"):
                payload = json.loads(messages[1].content)
                assert {c["id"] for c in payload["criteria"]} == {"condition", "goal"}
                observed = next(e for e in payload["evidence"] if e["id"] == "condition")
                assert observed["detail"]["exit_code"] == 0
                return ok(LlmResponse(content=json.dumps({"results": [
                    {"criterion_id": c["id"], "status": "failed", "reason": "Output does not establish the required condition",
                     "evidence_ids": ["condition"]} for c in payload["criteria"]
                ]}), usage=TokenUsage(10, 5, 15)))
            return await super().chat(messages, tools, cancellation, **kwargs)

    result = asyncio.run(run_generic_task(
        TaskRequest("Check required state", workspace=tmp_path), provider=OutcomeJudge([command()]),
        options=TaskRunOptions(plan_mode="off", max_steps=4),
    )).unwrap()
    acceptance = result.run_result.context.state.scratch["acceptance"]
    row = next(r for r in acceptance["results"] if r["criterion_id"] == "condition")
    assert row["status"] == "failed" and row["execution_check"]["status"] == "passed"
    assert acceptance["state"] != "passed"


def test_method_revision_preserves_original_assertions_and_cannot_relabel_conditions(tmp_path):
    assembly = TaskAssembly(TaskRequest("Produce required report", workspace=tmp_path), plan_mode="off")
    controller = assembly.acceptance
    original = {"id": "report", "description": "Report satisfies required content", "verifier": "artifact",
                "scope": [], "check": {"path": "answer.md", "contains": ["required fact"]}}
    controller.data["plan"] = controller.validate_plan({"criteria": [original]})
    controller.data["revision"] = 1
    controller.data["pending_verification"] = {"obsolete": True}
    runtime = SimpleNamespace(run_id="run", loop_id="loop", trace_id="trace", trace_sink=Sink())
    revised = {**original, "verifier": "semantic", "scope": [], "check": {}}
    asyncio.run(controller.revise({"base_revision": 1, "reason": "Use observed report evidence", "criteria": [revised]}, runtime))
    assert controller.data["revision"] == 2
    assert "pending_verification" not in controller.data
    assert controller.data["original_requirements"]["report"]["check"]["contains"] == ["required fact"]
    assert controller.data["method_review_ids"] == ["report"]
    assert controller.data["state"] == "not_ready"
    before = deepcopy(controller.snapshot())
    with pytest.raises(ValueError, match="removed or weakened"):
        asyncio.run(controller.revise({"base_revision": 2, "reason": "Relax", "criteria": [{**revised, "description": "Any text"}]}, runtime))
    assert controller.snapshot() == before
