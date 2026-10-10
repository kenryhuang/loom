import json

import pytest

from loom.core import Observation
from loom.runtime.planning import PlanningRuntime
from loom.runtime.workflow_routing import WorkflowRoutePhase, WorkflowRoutePolicy
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from loom.tasks.tools import make_task_tools
from tests.acceptance_fakes import FixtureVerifier


def started(policy=None):
    planning = PlanningRuntime("auto", route_policy=policy)
    planning.route.select_react("Direct task").unwrap()
    planning.route.mark_task_execution_started()
    return planning


def test_repeated_evidence_triggers_review_and_survives_checkpoint(tmp_path):
    planning = started()
    request = TaskRequest("Update repository and summarize changes", workspace=tmp_path)
    context = make_task_context(request, planning=planning).unwrap()
    for i in range(3):
        result = planning.observe_tool(
            context, Observation(str(i), "process_execute", {"argv": ["git", "pull"], "stdout": "Already up to date", "duration_ms": i}, "now")
        )
    assert result.metadata["controlFlow"]["stepBoundary"]
    assert planning.route.state.trigger == "repeated_work"
    restored = PlanningRuntime("auto")
    restored.restore(planning.snapshot()).unwrap()
    projected = restored.project_context(context)
    prompt = projected.identity.constraints[-1].description
    assert request.objective in prompt and "Already up to date" in prompt
    assert "evidence_gap" in prompt and "finish" in prompt
    assert {ref.id for ref in projected.affordances.tools} == {"enter_plan", "continue_react", "finish"}
    schema = next(ref.input_schema for ref in projected.affordances.tools if ref.id == "continue_react")
    assert set(schema["required"]) == {"reason", "evidence_gap", "next_action"}


@pytest.mark.asyncio
async def test_continue_review_requires_gap_and_action_without_losing_state():
    planning = started(WorkflowRoutePolicy(tool_call_threshold=1))
    planning.observe_tool(None, Observation("o", "read_file", {"content": "facts"}, "now"))
    continue_react = planning.wrap_tools({})["continue_react"]
    rejected = (await continue_react({"reason": "more checking"})).unwrap()
    assert rejected.value["accepted"] is False
    assert planning.route.state.phase is WorkflowRoutePhase.REVIEWING
    accepted = (await continue_react({"reason": "Required verification", "evidence_gap": "Remote branch not fetched", "next_action": "git fetch"})).unwrap()
    assert accepted.value["accepted"]
    assert "Remote branch not fetched" in planning.route.state.reason


@pytest.mark.asyncio
async def test_finish_during_review_completes_and_blocks_stale_tools(tmp_path):
    planning = started(WorkflowRoutePolicy(tool_call_threshold=1))
    planning.observe_tool(None, Observation("o", "read_file", {"content": "facts"}, "now"))
    handlers = planning.wrap_tools(make_task_tools(TaskRequest("Summarize", workspace=tmp_path)))
    empty = (await handlers["finish"]({"report": ""})).unwrap()
    assert not empty.value["completed"]
    assert planning.route.state.phase is WorkflowRoutePhase.REVIEWING
    finished = (await handlers["finish"]({"report": "Source-backed summary"})).unwrap()
    assert finished.value["completed"]
    assert planning.route.state.phase is WorkflowRoutePhase.COMPLETED
    stale = (await handlers["shell_execute"]({"command": "touch stale"})).unwrap()
    assert stale.value["accepted"] is False and not (tmp_path / "stale").exists()


@pytest.mark.asyncio
async def test_review_cannot_finish_without_required_source_verification():
    from loom.tasks.assembly import TaskAssembly

    request = TaskRequest(
        "Read https://example.org/ and summarize the source",
        task_spec={"tools": {"collections": ["web_research", "task_control"]}, "workflow": {"plugin": "legacy_planning", "mode": "auto"}},
    )
    assembly = TaskAssembly(request)
    planning = assembly.workflow
    planning.route.select_react("Fetch source").unwrap()
    planning.route.mark_task_execution_started()
    planning.route.request_review("capability_gap", "No source fetched").unwrap()
    result = (await assembly.handlers()["finish"]({"report": "Summary from memory"})).unwrap()
    assert result.value["accepted"] is False
    assert planning.route.state.phase is WorkflowRoutePhase.REVIEWING
    await assembly.close()


def test_reviews_remain_active_after_three_reviews_and_classify_exit_status():
    planning = started(WorkflowRoutePolicy(tool_call_threshold=100, cooldown_calls=0))
    for round_no in range(5):
        for i in range(2):
            planning.observe_tool(None, Observation(f"{round_no}-{i}", "process_execute", {"argv": ["failed", str(round_no)], "exit_code": 2}, "now"))
        assert planning.route.state.phase is WorkflowRoutePhase.REVIEWING
        assert planning.route.state.review_count == round_no + 1
        planning.route.select_react("Concrete gap").unwrap()
        planning.route.mark_task_execution_started()
    planning.observe_tool(None, Observation("empty", "process_execute", {"exit_code": 1, "status": "no_match", "ok": True}, "now"))
    assert planning.route.state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_task_can_finish_at_progress_review_without_extra_model_request(tmp_path):
    from loom.core import ok
    from loom.llm.api import LlmResponse, LlmToolCall

    (tmp_path / "evidence").write_text("Verified evidence")

    class Provider:
        verification_provider = FixtureVerifier()
        model = "test"
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                name, value = "continue_react", {"reason": "Read and summarize"}
            elif self.calls < 5:
                name, value = "read_file", {"path": "evidence"}
            else:
                assert self.calls == 5
                assert {t["function"]["name"] for t in tools} == {"finish", "enter_plan", "continue_react"}
                assert "Verified evidence" in str(messages)
                name, value = "finish", {"report": "Verified summary"}
            return ok(LlmResponse("", (LlmToolCall(str(self.calls), name, json.dumps(value)),)))

    provider = Provider()
    result = (await run_generic_task(TaskRequest("Summarize evidence", workspace=tmp_path), provider=provider, options=TaskRunOptions(max_steps=6))).unwrap()
    assert result.output == "Verified summary" and provider.calls == 5
    assert result.run_result.context.state.scratch["workflowRoute"]["phase"] == "completed"
