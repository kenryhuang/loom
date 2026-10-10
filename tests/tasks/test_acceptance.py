import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from loom.core import ok
from loom.llm import LlmResponse, LlmToolCall, TokenUsage
from loom.tasks.acceptance import AcceptanceController
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from loom.tasks.workspace_probe import probe


class Judge:
    model = "acceptance-fixture"

    def __init__(self, criteria=(), passed=True, finish=False):
        self.criteria, self.passed, self.finish = list(criteria), passed, finish
        self.calls = []

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        self.calls.append(messages)
        if messages[0].content.startswith("You design Loom task acceptance"):
            value = {"criteria": self.criteria, "unresolved_requirements": []}
        elif messages[0].content.startswith("You independently verify"):
            data = json.loads(messages[1].content)
            value = {
                "results": [
                    {"criterion_id": c["id"], "status": "passed" if self.passed else "failed", "reason": "Fixture assertion", "evidence_ids": ["candidate"]}
                    for c in data["criteria"]
                ]
            }
        elif self.finish:
            return ok(LlmResponse(tool_calls=(LlmToolCall("finish1", "finish", '{"report":"answer"}'),)))
        else:
            value = {"reasoning": "done", "action": {"kind": "none", "description": "answer", "input": {"report": "answer"}}}
        return ok(LlmResponse(content=json.dumps(value), usage=TokenUsage(10, 5, 15)))


class Sink:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)
        return ok(None)


@pytest.mark.parametrize("state", ["passed", "needs_repair"])
def test_mutating_tools_emit_invalidation_only_for_fresh_passed_evidence(tmp_path, state):
    assembly = TaskAssembly(TaskRequest("Write answer", workspace=tmp_path), plan_mode="off")
    controller, sink = assembly.acceptance, Sink()
    assembly._task_runtime = SimpleNamespace(run_id="run", loop_id="loop", trace_id="trace", trace_sink=sink)
    controller.data["plan"] = controller.validate_plan({"criteria": []})
    write = assembly.handlers()["write_file"]

    async def exercise():
        # Initial task work has no acceptance evidence to invalidate.
        for content in ("first", "second"):
            (await write({"path": "answer.md", "content": content})).unwrap()
        assert controller.data["write_epoch"] == 2
        assert not sink.events

        controller.data.update(
            state=state,
            binding=controller.fingerprint("answer"),
            results=[{"criterion_id": "goal", "status": "passed"}],
        )
        if state == "needs_repair":
            controller.data["results"].append({"criterion_id": "other", "status": "failed"})
        else:
            assert controller.applicable("answer")

        (await write({"path": "answer.md", "content": "third"})).unwrap()
        assert not controller.applicable("answer")
        assert controller.data["results"][0]["status"] == "stale"
        assert [e["type"] for e in sink.events] == ["acceptance.invalidated"]
        assert sink.events[0]["acceptance"]["results"][0]["status"] == "stale"

        # Further writes still change the freshness binding without duplicate events.
        (await write({"path": "answer.md", "content": "fourth"})).unwrap()
        assert controller.data["write_epoch"] == 4
        assert len(sink.events) == 1

        # Newly passed verification can be invalidated again.
        controller.data.update(state="passed", results=[{"criterion_id": "goal", "status": "passed"}])
        (await write({"path": "answer.md", "content": "fifth"})).unwrap()
        assert len(sink.events) == 2

    asyncio.run(exercise())


def test_direct_answer_and_finish_both_require_independent_acceptance(tmp_path):
    for finish in (False, True):
        provider, sink = Judge(finish=finish), Sink()
        result = asyncio.run(
            run_generic_task(
                TaskRequest("Answer the question", workspace=tmp_path), provider=provider, options=TaskRunOptions(plan_mode="off", max_steps=3), trace_sink=sink
            )
        ).unwrap()
        assert result.run_result.context.state.scratch["acceptance"]["state"] == "passed"
        assert result.output == "answer"
        assert len(provider.calls) == 3
        kinds = [e["type"] for e in sink.events]
        assert kinds.index("acceptance.plan.accepted") < kinds.index("acceptance.gate.passed") < kinds.index("run.completed")
        assert sum(e["type"] == "llm.requested" and e.get("usage_role") == "verification" for e in sink.events) == 2


def test_rejected_direct_answer_never_emits_completed(tmp_path):
    sink = Sink()
    result = asyncio.run(
        run_generic_task(
            TaskRequest("Answer", workspace=tmp_path), provider=Judge(passed=False), options=TaskRunOptions(plan_mode="off", max_steps=3), trace_sink=sink
        )
    ).unwrap()
    assert result.run_result.metrics.outcome == "paused"
    assert not any(e["type"] == "run.completed" for e in sink.events)


def test_probe_is_bounded_does_not_follow_symlinks_or_read_secrets(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]')
    (tmp_path / ".env").write_text("DO_NOT_READ=secret")
    (tmp_path / "outside").symlink_to("/tmp", target_is_directory=True)
    value = probe(tmp_path)
    assert value["configuration"][0]["path"] == "pyproject.toml"
    assert ".env" not in value["files"] and "outside/" not in value["files"]
    assert probe(None)["workspace"] == "absent"


def test_plan_cannot_omit_goal_and_checkpoint_invalidates_on_goal_or_file_change(tmp_path):
    (tmp_path / "answer.md").write_text("answer")
    assembly = TaskAssembly(TaskRequest("Write answer", workspace=tmp_path), plan_mode="off")
    controller = assembly.acceptance
    context = make_task_context(assembly.request, assembly=assembly, plan_mode="off").unwrap()
    controller.goal(context)
    plan = controller.validate_plan(
        {"criteria": [{"id": "file", "description": "Report exists", "verifier": "artifact", "scope": ["answer.md"], "check": {"path": "answer.md"}}]}
    )
    assert plan["criteria"][-1]["id"] == "goal"
    controller.data["plan"] = plan
    controller.data.update(state="passed", binding=controller.fingerprint("answer"))
    restored = AcceptanceController(assembly)
    restored.restore(controller.snapshot())
    assert restored.applicable("answer")
    (tmp_path / "answer.md").write_text("changed")
    assert not restored.applicable("answer")
    restored.goal(replace(context, goal=replace(context.goal, objective="Different goal")))
    assert restored.data["plan"] is None


def test_command_failure_is_repaired_and_checked_again(tmp_path):
    import sys

    (tmp_path / "value.txt").write_text("wrong")
    criterion = {
        "id": "regression",
        "description": "Actual value is correct",
        "verifier": "command",
        "scope": ["value.txt"],
        "check": {
            "tool_id": "process_execute",
            "input": {"argv": [sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'correct'"]},
        },
    }

    class Repair(Judge):
        def __init__(self):
            super().__init__([criterion])
            self.solver_calls = 0

        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if messages[0].content.startswith(("You design", "You independently")):
                return await super().chat(messages, tools=tools, **kwargs)
            self.solver_calls += 1
            if self.solver_calls == 2:
                return ok(LlmResponse(tool_calls=(LlmToolCall("repair", "write_file", '{"path":"value.txt","content":"correct"}'),)))
            return ok(LlmResponse(tool_calls=(LlmToolCall(f"finish-{self.solver_calls}", "finish", '{"report":"Corrected value"}'),)))

    sink = Sink()
    result = asyncio.run(
        run_generic_task(
            TaskRequest("Correct value.txt", workspace=tmp_path), provider=Repair(), options=TaskRunOptions(plan_mode="off", max_steps=6), trace_sink=sink
        )
    ).unwrap()
    assert result.run_result.context.state.scratch["acceptance"]["state"] == "passed"
    checks = [e["result"]["status"] for e in sink.events if e["type"] == "verification.completed" and e["result"]["criterion_id"] == "regression"]
    assert checks == ["failed", "passed"]
    assert (tmp_path / "value.txt").read_text() == "correct"
    commands = [e for e in sink.events if e["type"] == "tool.started" and e.get("tool_id") == "process_execute"]
    assert len(commands) == 2


def test_external_condition_blocks_without_repair_spinning(tmp_path):
    provider = Judge([{"id": "approval", "description": "Customer accepts", "verifier": "external", "scope": [], "check": {}}])
    result = asyncio.run(
        run_generic_task(
            TaskRequest("Deliver and obtain acceptance", workspace=tmp_path), provider=provider, options=TaskRunOptions(plan_mode="off", max_steps=3)
        )
    ).unwrap()
    acceptance = result.run_result.context.state.scratch["acceptance"]
    assert acceptance["state"] == "blocked"
    assert acceptance["attempts"] == 1
    assert len(provider.calls) == 2  # No semantic call when a required external fact is unavailable.


def test_invalid_semantic_evidence_cannot_pass(tmp_path):
    class InventedEvidence(Judge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if messages[0].content.startswith("You independently"):
                return ok(
                    LlmResponse(
                        content=json.dumps(
                            {"results": [{"criterion_id": "goal", "status": "passed", "reason": "Trust me", "evidence_ids": ["invented-oracle"]}]}
                        )
                    )
                )
            return await super().chat(messages, tools=tools, **kwargs)

    result = asyncio.run(
        run_generic_task(TaskRequest("Answer", workspace=tmp_path), provider=InventedEvidence(), options=TaskRunOptions(plan_mode="off", max_steps=3))
    ).unwrap()
    assert result.run_result.metrics.outcome == "paused"
    assert "registered evidence" in result.run_result.context.state.scratch["acceptance"]["reason"]


def test_plan_revision_cannot_remove_failed_requirement(tmp_path):
    import pytest

    assembly = TaskAssembly(TaskRequest("Write answer", workspace=tmp_path), plan_mode="off")
    controller = assembly.acceptance
    required = {"id": "file", "description": "Report exists", "verifier": "artifact", "scope": ["answer.md"], "check": {"path": "answer.md"}}
    controller.data["plan"] = controller.validate_plan({"criteria": [required]})
    controller.data["revision"] = 1
    with pytest.raises(ValueError, match="removed or weakened"):
        asyncio.run(controller.revise({"base_revision": 1, "reason": "Test failed", "criteria": []}, None))


def test_no_workspace_uses_semantic_acceptance():
    result = asyncio.run(
        run_generic_task(
            TaskRequest("Answer", task_spec={"tools": {"collections": []}, "workflow": {"plugin": "dynamic"}}),
            provider=Judge(),
            options=TaskRunOptions(max_steps=3),
        )
    ).unwrap()
    assert result.run_result.context.state.scratch["acceptance"]["state"] == "passed"
    assert result.run_result.context.state.scratch["execution_plugins"]["acceptance"]["profile"]["workspace"] == "absent"


def test_terminal_checkpoint_revalidates_current_files_without_replaying_solver(tmp_path):
    from loom.llm.managed_step import ManagedStep
    from loom.runtime import create, create_runtime_registry, step
    from loom.tasks.runner import make_task_loop
    from tests.llm.test_managed_step import Execution

    class Journal(Execution):
        def rpc(self, kind, payload):
            assert kind == "publish_artifact"
            from loom.tasks.workspace_probe import digest

            return {"sha256": digest(payload), "kind": payload["kind"]}

    async def scenario():
        (tmp_path / "answer.md").write_text("answer")
        request = TaskRequest("Write answer", workspace=tmp_path)
        provider = Judge(
            [{"id": "document", "description": "Report delivered", "verifier": "artifact", "scope": ["answer.md"], "check": {"path": "answer.md"}}]
        )
        journal = Journal()

        async def execute():
            assembly = TaskAssembly(request, plan_mode="off", execution=journal)
            assembly.configure_acceptance(provider)
            context = make_task_context(request, assembly=assembly, plan_mode="off").unwrap()
            if journal.checkpoint:
                assembly.restore(journal.checkpoint["plugin_states"])
                context = journal.checkpoint["context"]
            definition = make_task_loop(request, provider)
            managed = ManagedStep(provider, journal, assembly=assembly, planning=assembly.workflow)
            definition = assembly.wrap_loop(replace(definition, step=managed))
            handle = create(definition, registry=create_runtime_registry(tools=assembly.handlers())).unwrap()
            try:
                return (await step(handle, context, trace_sink=Sink())).unwrap()
            finally:
                await assembly.close()

        first = await execute()
        assert first.control.kind == "completed"
        count = len(provider.calls)
        (tmp_path / "answer.md").unlink()
        resumed = await execute()
        assert resumed.control.kind == "paused"
        assert len(provider.calls) == count  # Deterministic failure needs neither solver replay nor a judge call.
        assert "terminal" not in journal.checkpoint

    asyncio.run(scenario())


def test_interrupted_command_retains_operation_identity_for_journal_reconciliation(tmp_path):
    from loom.core import err, make_loom_error

    assembly = TaskAssembly(TaskRequest("Check", workspace=tmp_path), plan_mode="off")
    controller = assembly.acceptance
    criterion = {
        "id": "check",
        "description": "Check",
        "verifier": "command",
        "scope": ["missing.py"],
        "check": {"tool_id": "process_execute", "input": {"argv": ["python", "missing.py"]}},
    }
    controller.data["plan"] = controller.validate_plan({"criteria": [criterion]})
    controller.data["candidate"] = "done"
    identifiers = []

    class Runtime:
        async def call_tool(self, tool, value, metadata):
            identifiers.append(metadata["tool_call_id"])
            return err(make_loom_error("EXECUTION_UNKNOWN", "Interrupted operation requires reconciliation", retryable=False))

    result, _ = asyncio.run(controller.check(criterion, Runtime()))
    assert result["status"] == "blocked"
    restored = AcceptanceController(assembly)
    restored.restore(controller.snapshot())
    asyncio.run(restored.check(criterion, Runtime()))
    assert identifiers[0] == identifiers[1]
    assert restored.data["pending_operations"]["check"] == identifiers[0]


def test_final_deterministic_node_can_enter_model_repair(tmp_path):
    criterion = {
        "id": "file",
        "description": "Correct deliverable",
        "verifier": "artifact",
        "scope": ["answer.txt"],
        "check": {"path": "answer.txt", "contains": ["correct"]},
    }

    class Repair(Judge):
        def __init__(self):
            super().__init__([criterion])
            self.solver_calls = 0

        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if messages[0].content.startswith(("You design", "You independently")):
                return await super().chat(messages, tools, cancellation, **kwargs)
            self.solver_calls += 1
            if self.solver_calls == 1:
                return ok(LlmResponse(tool_calls=(LlmToolCall("repair", "write_file", '{"path":"answer.txt","content":"correct"}'),)))
            return await super().chat(messages, tools, cancellation, **kwargs)

    spec = {
        "session_environment": {"plugin": "session", "resources": [{"id": "workspace", "kind": "directory", "uri": str(tmp_path), "access": "read-write"}]},
        "tools": {"collections": ["filesystem", "task_control"]},
        "workflow": {
            "plugin": "dynamic",
            "nodes": [
                {
                    "id": "deliver",
                    "objective": "Deliver",
                    "executor_kind": "tool",
                    "executor_config": {"tool_id": "write_file", "input": {"path": "answer.txt", "content": "wrong"}},
                }
            ],
        },
    }
    result = asyncio.run(run_generic_task(TaskRequest("Write correct", task_spec=spec), provider=Repair(), options=TaskRunOptions(max_steps=5))).unwrap()
    assert result.run_result.metrics.outcome == "pass"
    assert (tmp_path / "answer.txt").read_text() == "correct"
    nodes = result.run_result.context.state.scratch["workflow"]["workflow"]["nodes"]
    assert nodes[-1]["id"].startswith("acceptance-repair-")
    assert all(n["status"] == "succeeded" for n in nodes)


class PlanRepairJudge(Judge):
    def __init__(self, proposals):
        super().__init__()
        self.proposals = proposals
        self.plan_payloads = []

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        if messages[0].content.startswith("You design Loom task acceptance"):
            self.calls.append(messages)
            self.plan_payloads.append(json.loads(messages[1].content))
            value = self.proposals[min(len(self.plan_payloads) - 1, len(self.proposals) - 1)]
            return ok(LlmResponse(content=value if isinstance(value, str) else json.dumps(value), usage=TokenUsage(10, 5, 15)))
        return await super().chat(messages, tools, cancellation, **kwargs)


def malformed_process_plan():
    return {
        "criteria": [
            {
                "id": "c1",
                "description": "Check actual project data",
                "verifier": "command",
                "scope": ["value.txt"],
                "check": {"tool_id": "process_execute", "input": "cat value.txt"},
            }
        ]
    }


def test_invalid_process_input_is_corrected_before_execution_using_actual_schema(tmp_path):
    import sys

    (tmp_path / "value.txt").write_text("correct")
    corrected = malformed_process_plan()
    corrected["criteria"][0]["check"]["input"] = {"argv": [sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'correct'"]}
    judge, sink = PlanRepairJudge([malformed_process_plan(), corrected]), Sink()
    result = asyncio.run(
        run_generic_task(TaskRequest("Check project data", workspace=tmp_path), provider=judge, options=TaskRunOptions(plan_mode="off"), trace_sink=sink)
    ).unwrap()
    assert result.run_result.metrics.outcome == "pass"
    assert len(judge.calls) == 4  # Plan, bounded schema correction, solver, semantic verification.
    schemas = {tool["id"]: tool["input_schema"] for tool in judge.plan_payloads[0]["command_tools"]}
    assert schemas["process_execute"]["type"] == "object"
    assert "argv" in schemas["process_execute"]["properties"]
    assert "command" in schemas["shell_execute"]["properties"]
    correction = judge.plan_payloads[1]["repair"]
    assert "Criterion c1: process_execute check.input" in correction["validation_error"]
    assert correction["previous_proposal"] == malformed_process_plan()
    kinds = [e["type"] for e in sink.events]
    assert kinds.count("acceptance.plan.rejected") == kinds.count("acceptance.plan.repairing") == 1
    assert "acceptance.gate.blocked" not in kinds
    assert kinds.index("acceptance.plan.accepted") < kinds.index("tool.started")


def test_repeated_invalid_plan_pauses_with_actionable_reason_after_one_correction(tmp_path):
    judge, sink = PlanRepairJudge([malformed_process_plan()]), Sink()
    result = asyncio.run(
        run_generic_task(TaskRequest("Check", workspace=tmp_path), provider=judge, options=TaskRunOptions(plan_mode="off"), trace_sink=sink)
    ).unwrap()
    assert result.run_result.metrics.outcome == "paused"
    assert len(judge.calls) == 2
    state = result.run_result.context.state.scratch["acceptance"]
    assert state["attempts"] == 0 and state["plan"] is None
    assert "after one correction" in state["reason"] and "Criterion c1" in state["reason"]
    assert not any(e["type"] == "tool.started" for e in sink.events)


def test_malformed_json_plan_is_corrected_but_retry_cannot_bypass_budget(tmp_path):
    judge = PlanRepairJudge(["```", {"criteria": []}])
    result = asyncio.run(run_generic_task(TaskRequest("Answer", workspace=tmp_path), provider=judge, options=TaskRunOptions(plan_mode="off"))).unwrap()
    assert result.run_result.metrics.outcome == "pass"
    assert "not valid JSON" in judge.plan_payloads[1]["repair"]["validation_error"]
    limited = PlanRepairJudge(["not json", {"criteria": []}])
    result = asyncio.run(
        run_generic_task(
            TaskRequest("Answer", task_spec={"tools": {"collections": []}, "workflow": {"plugin": "dynamic"}, "acceptance": {"max_model_calls": 1}}),
            provider=limited,
        )
    ).unwrap()
    assert result.run_result.metrics.outcome == "paused" and len(limited.calls) == 1
    assert "budget exhausted" in result.run_result.context.state.scratch["acceptance"]["reason"]
