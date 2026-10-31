import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from loom.core import ok
from loom.llm import LlmResponse, LlmToolCall
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from tests.tasks.test_acceptance import Judge, Sink


class EvidenceJudge(Judge):
    """The report cannot pass until real read_file output reaches the verifier."""

    def __init__(self, *, finish=True, repair=True, gap_kind="missing_evidence"):
        super().__init__([{"id": "source", "description": "Report supported by source.txt", "verifier": "semantic",
                           "scope": ["source.txt"], "check": {}}])
        self.use_finish, self.repair, self.gap_kind = finish, repair, gap_kind
        self.solver_calls = self.verifications = 0
        self.feedback_seen = False

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        if messages[0].content.startswith("You design"):
            return await super().chat(messages, tools, cancellation, **kwargs)
        if messages[0].content.startswith("You independently"):
            self.verifications += 1
            payload = json.loads(messages[1].content)
            observed = any(e.get("tool") == "read_file" and "verifiable source fact" in (e.get("output") or "") for e in payload["evidence"])
            return ok(LlmResponse(json.dumps({"results": [{
                "criterion_id": c["id"], "status": "passed" if observed else "blocked",
                "reason": "Supported" if observed else "Source content is missing", "evidence_ids": ["candidate"],
                "gap": None if observed else {"kind": self.gap_kind, "targets": [{"kind": "workspace_path", "value": "source.txt"}],
                                               "required_fact": "Current source fact", "suggested_action": "Read source.txt"},
            } for c in payload["criteria"]]})))
        self.solver_calls += 1
        if self.solver_calls > 1:
            self.feedback_seen |= "Current source fact" in " ".join(m.content or "" for m in messages)
        if self.solver_calls == 2 and self.repair:
            return ok(LlmResponse(tool_calls=(LlmToolCall("read-source", "read_file", '{"path":"source.txt"}'),)))
        if self.use_finish:
            return ok(LlmResponse(tool_calls=(LlmToolCall(f"finish-{self.solver_calls}", "finish", '{"report":"Source report"}'),)))
        return ok(LlmResponse(json.dumps({"reasoning": "done", "action": {"kind": "none", "description": "done", "input": {"report": "Source report"}}})))


@pytest.mark.parametrize("finish", [False, True])
@pytest.mark.parametrize("plugin", ["legacy_planning", "dynamic", "adaptive_dag"])
def test_missing_evidence_repairs_in_next_task_step_without_resume(tmp_path, finish, plugin):
    (tmp_path / "source.txt").write_text("verifiable source fact")
    judge, sink = EvidenceJudge(finish=finish), Sink()
    spec = None if plugin == "legacy_planning" else {
        "workflow": {"plugin": plugin}, "tools": {"collections": ["filesystem", "task_control"]},
        "session_environment": {"plugin": "session", "resources": [{"id": "workspace", "kind": "directory", "uri": str(tmp_path)}]},
    }
    result = asyncio.run(run_generic_task(TaskRequest("Report current source", workspace=tmp_path, task_spec=spec), provider=judge,
                                         options=TaskRunOptions(plan_mode="off", max_steps=6), trace_sink=sink)).unwrap()
    acceptance = result.run_result.context.state.scratch["acceptance"]
    assert result.run_result.metrics.outcome == "pass"
    assert acceptance["feedback"]["state"] == "resolved"
    assert acceptance["budget"]["repair_rounds"] == 1
    assert judge.feedback_seen and judge.verifications == 2
    assert sum(e["type"] == "acceptance.feedback.applied" for e in sink.events) == 1
    assert sum(e["type"] == "tool.completed" and e.get("tool_id") == "read_file" for e in sink.events) == 1


@pytest.mark.parametrize("kind,code", [("external_dependency", "ACCEPTANCE_EXTERNAL_DEPENDENCY"), ("unknown", "ACCEPTANCE_UNKNOWN_GAP")])
def test_external_and_unknown_gaps_do_not_auto_resume(tmp_path, kind, code):
    judge = EvidenceJudge(gap_kind=kind)
    result = asyncio.run(run_generic_task(TaskRequest("Report source", workspace=tmp_path), provider=judge,
                                         options=TaskRunOptions(plan_mode="off", max_steps=5))).unwrap()
    assert result.run_result.metrics.outcome == "paused"
    assert judge.solver_calls == 1
    assert result.run_result.context.state.scratch["acceptance"]["decision"]["reason_code"] == code


def test_identical_report_after_repair_pauses_without_paid_reverification(tmp_path):
    judge = EvidenceJudge(repair=False)
    result = asyncio.run(run_generic_task(TaskRequest("Report source", workspace=tmp_path), provider=judge,
                                         options=TaskRunOptions(plan_mode="off", max_steps=8))).unwrap()
    gate = result.run_result.context.state.scratch["acceptance"]
    assert result.run_result.metrics.outcome == "paused"
    assert gate["decision"]["reason_code"] == "ACCEPTANCE_NO_PROGRESS"
    assert gate["budget"]["repair_rounds"] == 1
    assert judge.verifications == 1


@pytest.mark.parametrize("plugin", ["legacy_planning", "dynamic", "adaptive_dag"])
def test_workflow_feedback_is_idempotent_and_keeps_terminal_work(tmp_path, plugin):
    from loom.tasks.acceptance_feedback import decide
    from loom.workflows.legacy import LegacyPlanningWorkflow

    spec = {"workflow": {"plugin": plugin, **({"mode": "force"} if plugin == "legacy_planning" else {})}}
    assembly = TaskAssembly(TaskRequest("Report source", task_spec=spec), plan_mode="off")
    workflow = assembly.workflow
    if isinstance(workflow, LegacyPlanningWorkflow):
        workflow.controller.submit("Read source", ["Read source"]).unwrap()
        item = workflow.controller.state.items[0]
        workflow.controller.update("done", [{"id": item.id, "content": item.content, "status": "completed"}]).unwrap()
        workflow.controller.complete().unwrap()
        original = workflow.controller.state.items
    else:
        workflow.controller.propose({"base_revision": 0, "reason": "initial", "nodes": [{"id": "done", "objective": "Read source"}]})
        workflow.controller.nodes[0]["status"] = "succeeded"
        original = dict(workflow.controller.nodes[0])
    context = make_task_context(assembly.request, assembly=assembly).unwrap()
    gate = assembly.acceptance
    gate.goal(context)
    gate.data.update(plan=gate.validate_plan({"criteria": []}), state="blocked", candidate="Report", results=[{
        "criterion_id": "goal", "status": "blocked", "reason": "Missing source", "gap": {
            "kind": "missing_evidence", "targets": [], "required_fact": "Read missing source", "suggested_action": "Read"}}])
    feedback = gate.data.get("feedback")
    assert decide(gate).outcome == "repair"
    feedback = gate.data["feedback"]
    first = workflow.apply_task_feedback(feedback)
    snapshot = workflow.snapshot()
    workflow.restore(snapshot).unwrap()
    assert workflow.apply_task_feedback(feedback) == first
    assert workflow.snapshot() == snapshot
    if isinstance(workflow, LegacyPlanningWorkflow):
        assert workflow.controller.state.items[:-1] == original
        assert "Read missing source" in workflow.controller.state.items[-1].content
    else:
        assert workflow.controller.nodes[0] == original
        assert "Read missing source" in workflow.controller.nodes[-1]["objective"]
    asyncio.run(assembly.close())


def test_committed_feedback_transition_replays_without_verifier_or_duplicate_plan(tmp_path):
    from loom.runtime import create, create_runtime_registry, step
    from loom.tasks.acceptance_feedback import decide
    from loom.tasks.runner import make_task_loop
    from tests.llm.test_managed_step import Execution

    async def exercise():
        request = TaskRequest("Report", workspace=tmp_path)
        assembly = TaskAssembly(request, plan_mode="off")
        context = make_task_context(request, assembly=assembly, plan_mode="off").unwrap()
        gate = assembly.acceptance
        gate.goal(context)
        gate.data.update(plan=gate.validate_plan({"criteria": []}), state="needs_repair", candidate="report",
                         results=[{"criterion_id": "goal", "status": "failed", "reason": "Fix report"}])
        decide(gate)
        execution = Execution()
        assembly.execution = execution
        assembly._acceptance_cp = {"context": context, "observations": []}
        runtime = SimpleNamespace(run_id=context.run_id, loop_id="loop", trace_id="trace", trace_sink=Sink())
        result = (await assembly.acceptance_blocked_result(context, runtime, "Fix report")).unwrap()
        saved = execution.checkpoint
        assert saved["task_feedback_transition"].control.kind == "continue"
        replacement = TaskAssembly(request, plan_mode="off")
        replacement.restore(saved["plugin_states"])
        replacement.execution = execution

        async def forbidden(*args):
            pytest.fail("Committed transition must be replayed before executing another step")

        definition = replacement.wrap_loop(replace(make_task_loop(request, Judge()), step=forbidden))
        handle = create(definition, registry=create_runtime_registry()).unwrap()
        replay = (await step(handle, saved["context"], trace_sink=Sink())).unwrap()
        assert replay == result
        assert replacement.acceptance.data["repair_rounds"] == 1
        assert len([o for o in replay.context.state.observations if o.source == "acceptance"]) == 1
        assembly.execution = replacement.execution = None
        await assembly.close()
        await replacement.close()

    asyncio.run(exercise())


def test_truncated_evidence_view_is_repaired_without_rereading_source(tmp_path):
    async def exercise():
        (tmp_path / "source.txt").write_text("x" * 6500 + " verifiable source fact")
        judge = EvidenceJudge(gap_kind="insufficient_evidence_view")
        assembly = TaskAssembly(TaskRequest("Report source", workspace=tmp_path), plan_mode="off")
        assembly.configure_acceptance(judge)
        context = make_task_context(assembly.request, assembly=assembly, plan_mode="off").unwrap()
        runtime = SimpleNamespace(run_id=context.run_id, loop_id="loop", trace_id="trace", trace_sink=Sink(), cancellation=None)
        assembly.context, assembly._task_runtime = context, runtime
        try:
            await assembly.acceptance.prepare(context, runtime)
            assert (await assembly.handlers()["read_file"]({"path": "source.txt"})).ok
            decision = await assembly.request_completion("Source report")
            assert decision.outcome == "accept"
            assert judge.verifications == 2
            assert len(assembly._acceptance_calls) == 1
            assert assembly.acceptance.data["repair_rounds"] == 0
        finally:
            await assembly.close()

    asyncio.run(exercise())


def test_restored_evidence_is_stale_after_file_changes(tmp_path):
    async def exercise():
        path = tmp_path / "source.txt"
        path.write_text("verifiable source fact")
        assembly = TaskAssembly(TaskRequest("Report source", workspace=tmp_path), plan_mode="off")
        context = make_task_context(assembly.request, assembly=assembly, plan_mode="off").unwrap()
        assembly.acceptance.goal(context)
        try:
            assert (await assembly.handlers()["read_file"]({"path": "source.txt"})).ok
            before = assembly.snapshot()
            path.write_text("changed source")
            assembly.restore(before)
            row = next(r for r in assembly.acceptance_evidence() if r.get("tool") == "read_file")
            assert row["stale"] and row["output"] is None
            assert assembly._acceptance_calls[0]["artifact"]["sha256"] == before["acceptance_calls"][0]["artifact"]["sha256"]
        finally:
            await assembly.close()

    asyncio.run(exercise())


def test_zero_repair_budget_preserves_gaps_without_scheduling(tmp_path):
    spec = {"acceptance": {"max_repairs": 0}, "workflow": {"plugin": "legacy_planning", "mode": "off"},
            "session_environment": {"plugin": "session", "resources": [{"id": "workspace", "kind": "directory", "uri": str(tmp_path)}]},
            "tools": {"collections": ["filesystem", "task_control"]}}
    judge = EvidenceJudge()
    result = asyncio.run(run_generic_task(TaskRequest("Report source", task_spec=spec), provider=judge,
                                         options=TaskRunOptions(max_steps=5))).unwrap()
    gate = result.run_result.context.state.scratch["acceptance"]
    assert result.run_result.metrics.outcome == "paused"
    assert gate["decision"]["reason_code"] == "ACCEPTANCE_REPAIR_LIMIT"
    assert gate["decision"]["blockers"] and gate["budget"]["repair_rounds"] == 0
    assert judge.solver_calls == 1


def test_saved_assessment_restores_feedback_without_another_paid_verification(tmp_path):
    async def exercise():
        request = TaskRequest("Report source", workspace=tmp_path)
        judge = EvidenceJudge()
        assembly = TaskAssembly(request, plan_mode="off")
        assembly.configure_acceptance(judge)
        context = make_task_context(request, assembly=assembly, plan_mode="off").unwrap()
        runtime = SimpleNamespace(run_id=context.run_id, loop_id="loop", trace_id="trace", trace_sink=Sink(), cancellation=None)
        restored = TaskAssembly(request, plan_mode="off")
        restored.configure_acceptance(judge)
        try:
            # Crash boundary: assessment committed, no decision/feedback yet.
            assert not await assembly.acceptance.verify(context, runtime, "Source report")
            assert assembly.acceptance.data["feedback"] is None
            restored._local_artifacts = assembly._local_artifacts
            restored.restore(assembly.snapshot())
            restored.context, restored._task_runtime = context, runtime
            assert (await restored.request_completion("Source report", origin="resume_completion")).outcome == "repair"
            assert judge.verifications == 1
            applied = (await restored.acceptance_blocked_result(context, runtime, "Source missing")).unwrap()
            assert applied.control.kind == "continue"
            assert restored.acceptance.data["repair_rounds"] == 1
        finally:
            await restored.close()
            await assembly.close()

    asyncio.run(exercise())


def test_uncommitted_workflow_application_replays_with_same_node_identity(tmp_path):
    from loom.tasks.acceptance_feedback import decide
    from tests.llm.test_managed_step import Execution

    class CrashBeforeCommit(Execution):
        def boundary(self, checkpoint):
            raise RuntimeError("worker lost before commit")

    async def exercise():
        request = TaskRequest("Report source", task_spec={"workflow": {"plugin": "dynamic"}})
        assembly = TaskAssembly(request)
        context = make_task_context(request, assembly=assembly).unwrap()
        gate = assembly.acceptance
        gate.goal(context)
        gate.data.update(plan=gate.validate_plan({"criteria": []}), state="needs_repair", candidate="report",
                         results=[{"criterion_id": "goal", "status": "failed", "reason": "Fix report"}])
        decide(gate)
        committed = assembly.snapshot()
        runtime = SimpleNamespace(run_id=context.run_id, loop_id="loop", trace_id="trace", trace_sink=Sink())
        assembly.execution = CrashBeforeCommit()
        assembly._acceptance_cp = {"context": context, "observations": []}
        try:
            with pytest.raises(RuntimeError, match="before commit"):
                await assembly.acceptance_blocked_result(context, runtime, "Fix report")
            uncommitted_id = assembly.workflow.controller.nodes[-1]["id"]
            assert not runtime.trace_sink.events
            assembly.restore(committed)
            assembly.execution = Execution()
            assembly._acceptance_cp = {"context": context, "observations": []}
            result = (await assembly.acceptance_blocked_result(context, runtime, "Fix report")).unwrap()
            assert result.control.kind == "continue"
            assert assembly.workflow.controller.nodes[-1]["id"] == uncommitted_id
            assert len(assembly.workflow.controller.nodes) == 1
            assert gate.data["repair_rounds"] == 1
        finally:
            assembly.execution = None
            await assembly.close()

    asyncio.run(exercise())
