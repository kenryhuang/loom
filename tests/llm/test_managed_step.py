import asyncio
import json

import pytest

from loom.core import Observation, ok
from loom.llm.api import LlmResponse, LlmToolCall, TokenUsage
from loom.llm.managed_step import ManagedStep
from loom.runtime import create, create_runtime_registry, step
from loom.runtime.checkpoints import decode, encode
from loom.runtime.planning import PlanningRuntime
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context, make_task_loop


def final(report="done"):
    return LlmResponse(json.dumps({"reasoning": "verified", "action": {"kind": "none", "description": "Complete", "input": {"report": report}}}))


class Provider:
    model = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls.append(list(messages))
        return ok(self.responses.pop(0))


class Execution:
    def __init__(self):
        self.checkpoint = None
        self.operations = []
        self.pause_after_tool = False
        self.inputs = []
        self.answer = None

    def boundary(self, checkpoint):
        self.checkpoint = decode(encode(checkpoint))
        control = {"kind": "paused", "reason": "pause"} if self.pause_after_tool and self.operations else None
        inputs, self.inputs = self.inputs, []
        return {"control": control, "inputs": inputs, "input_answer": self.answer}

    def operation_start(self, call):
        return None

    def operation_finish(self, call, result):
        self.operations.append(call.id)

    def request_input(self, call, question):
        return {"id": "question-1", "question": question["question"]}


async def run_managed(tmp_path, provider, execution, tools=None, planning=None, limits=None, context=None):
    request = TaskRequest("Maintain", workspace=tmp_path)
    context = context or make_task_context(request, plan_mode="off").unwrap()
    managed = ManagedStep(provider, execution, planning=planning, limits=limits)
    definition = make_task_loop(request, provider)
    from dataclasses import replace

    definition = replace(definition, step=managed)
    if planning:
        definition = planning.wrap_loop(definition)
        tools = planning.wrap_tools(tools or {})
    handle = create(definition, registry=create_runtime_registry(tools=tools or {})).unwrap()
    result = await step(handle, context, trace_id=(execution.checkpoint or {}).get("trace_id"))
    return result


def test_pause_after_tool_resume_preserves_results_without_replaying(tmp_path):
    async def scenario():
        calls = []

        async def read(value, _options):
            calls.append(value)
            return ok(Observation("read-1", "read_file", {"content": "evidence"}, "now"))

        provider = Provider([LlmResponse("", (LlmToolCall("call-1", "read_file", '{"path":"a"}'),)), final()])
        execution = Execution()
        execution.pause_after_tool = True
        first = await run_managed(tmp_path, provider, execution, {"read_file": read})
        assert first.ok and first.value.control.kind == "paused"
        assert len(first.value.context.state.observations) == 1
        execution.pause_after_tool = False
        resumed = await run_managed(tmp_path, provider, execution, {"read_file": read})
        assert resumed.ok and resumed.value.control.kind == "completed"
        assert len(calls) == 1
        assert len(provider.calls) == 2
        assert any(m.role == "tool" and m.tool_call_id == "call-1" for m in provider.calls[-1])

    asyncio.run(scenario())


def test_human_answer_completes_pending_tool_exactly_once(tmp_path):
    async def scenario():
        provider = Provider([LlmResponse("", (LlmToolCall("ask-1", "request_input", '{"question":"Which module?"}'),)), final()])
        execution = Execution()
        first = await run_managed(tmp_path, provider, execution)
        assert first.ok and first.value.control.kind == "waiting_input"
        assert first.value.control.request_id == "question-1"
        execution.answer = {"request_id": "question-1", "answer": "Payments", "superseded": False}
        resumed = await run_managed(tmp_path, provider, execution)
        assert resumed.ok and resumed.value.control.kind == "completed"
        assert execution.operations == ["ask-1"]
        assert any(m.tool_call_id == "ask-1" and "Payments" in m.content for m in provider.calls[-1])

    asyncio.run(scenario())


def test_steering_interrupts_unexecuted_native_tool_batch(tmp_path):
    async def scenario():
        execution = Execution()
        calls = []

        async def read(value, _options):
            calls.append(value["path"])
            execution.inputs = [{"seq": 10, "content": "Do not read the second file"}]
            return ok(Observation("o", "read_file", {}, "now"))

        provider = Provider(
            [
                LlmResponse("", (LlmToolCall("one", "read_file", '{"path":"one"}'), LlmToolCall("two", "read_file", '{"path":"two"}'))),
                final(),
            ]
        )
        result = await run_managed(tmp_path, provider, execution, {"read_file": read})
        assert result.ok
        assert calls == ["one"]
        messages = provider.calls[-1]
        assert {m.tool_call_id for m in messages if m.role == "tool"} == {"one", "two"}
        assert any(m.role == "user" and "Do not read" in m.content for m in messages)
        assert "User guidance:\nDo not read the second file" in messages[0].content
        assert "Maintain" in messages[0].content

    asyncio.run(scenario())


def test_json_tool_actions_and_recoverable_failures(tmp_path):
    async def scenario():
        from loom.core import err, make_loom_error

        async def read(_value, _options):
            return err(make_loom_error("TOOL_FAILED", "missing", retryable=False))

        content = json.dumps({"action": {"kind": "tool", "target": "read_file", "description": "Read", "input": {"path": "missing"}}})
        result = await run_managed(tmp_path, Provider([LlmResponse(content), final()]), Execution(), {"read_file": read})
        assert result.ok
        assert result.value.context.state.observations[0].value["ok"] is False

    asyncio.run(scenario())


def test_window_and_token_limits_suspend_instead_of_passing(tmp_path):
    async def scenario():
        result = await run_managed(tmp_path, Provider([final()]), Execution(), limits={"max_window_chars": 10})
        assert result.ok and result.value.control.kind == "paused"
        response = final()
        provider = Provider([LlmResponse(response.content, usage=TokenUsage(5, 5, 10))])
        result = await run_managed(tmp_path, provider, Execution(), limits={"max_tokens": 5})
        assert result.ok and result.value.control.kind == "paused"

    asyncio.run(scenario())


def test_input_after_response_prevents_dispatching_old_tools(tmp_path):
    async def scenario():
        class SteeringExecution(Execution):
            def boundary(self, cp):
                if cp["phase"] == "after_llm" and cp["response"].tool_calls:
                    self.inputs = [{"seq": 5, "content": "Cancel this read"}]
                return super().boundary(cp)

        calls = []

        async def read(value, _options):
            calls.append(value)
            return ok(Observation("o", "read_file", {}, "now"))

        provider = Provider([LlmResponse("", (LlmToolCall("old", "read_file", '{"path":"old"}'),)), final()])
        result = await run_managed(tmp_path, provider, SteeringExecution(), {"read_file": read})
        assert result.ok
        assert calls == []
        assert any(m.tool_call_id == "old" and "interrupted" in m.content for m in provider.calls[-1])

    asyncio.run(scenario())


def test_workflow_boundary_and_call_budget_are_explicit_controls(tmp_path):
    async def scenario():
        planning = PlanningRuntime("auto")
        provider = Provider([LlmResponse("", (LlmToolCall("route", "continue_react", '{"reason":"direct"}'),))])
        result = await run_managed(tmp_path, provider, Execution(), planning=planning)
        assert result.ok and result.value.control.kind == "continue"
        assert planning.route.state.phase.value == "react"
        provider = Provider([LlmResponse("", (LlmToolCall("read", "read_file", '{"path":"a"}'),))])

        async def read(value, _options):
            return ok(Observation("o", "read_file", value, "now"))

        result = await run_managed(tmp_path, provider, Execution(), {"read_file": read}, limits={"max_llm_calls": 1})
        assert result.ok and result.value.control.kind == "paused"

    asyncio.run(scenario())


def test_committed_final_checkpoint_finishes_without_another_model_call(tmp_path):
    async def scenario():
        provider = Provider([final("saved report")])
        execution = Execution()
        first = await run_managed(tmp_path, provider, execution)
        assert first.ok
        restored = await run_managed(tmp_path, provider, execution)
        assert restored.ok and restored.value.output == first.value.output
        assert len(provider.calls) == 1

    asyncio.run(scenario())


def test_native_finish_stops_batch_and_resume_without_another_call(tmp_path):
    async def scenario():
        from loom.tasks.tools import make_task_tools

        provider = Provider(
            [
                LlmResponse(
                    "",
                    (
                        LlmToolCall("finish", "finish", '{"report":"Committed answer"}'),
                        LlmToolCall("stale", "shell_execute", '{"command":"touch stale"}'),
                    ),
                ),
            ]
        )
        execution = Execution()
        tools = make_task_tools(TaskRequest("Answer", workspace=tmp_path))

        async def finish(_value, _options):
            # Custom native tools need not know Loom's step-boundary metadata.
            return ok(Observation("finished", "finish", {"completed": True, "report": "Committed answer"}, "now"))

        tools["finish"] = finish
        first = await run_managed(tmp_path, provider, execution, tools)
        assert first.ok and first.value.control.kind == "completed"
        assert first.value.output == "Committed answer"
        assert not (tmp_path / "stale").exists()
        restored = await run_managed(tmp_path, provider, execution)
        assert restored.ok and restored.value.output == "Committed answer"
        assert execution.operations == ["finish"] and len(provider.calls) == 1
        execution.checkpoint.pop("terminal")
        execution.checkpoint["phase"] = "before_llm"
        legacy = await run_managed(tmp_path, provider, execution)
        assert legacy.ok and legacy.value.output == "Committed answer" and len(provider.calls) == 1

    asyncio.run(scenario())


def test_managed_progress_review_can_finish_from_committed_evidence(tmp_path):
    async def scenario():
        from loom.runtime.workflow_routing import WorkflowRoutePolicy
        from loom.tasks.tools import make_task_tools

        (tmp_path / "evidence").write_text("Already up to date; verified changes")
        planning = PlanningRuntime("auto", route_policy=WorkflowRoutePolicy(tool_call_threshold=1))
        planning.route.select_react("Inspect and summarize").unwrap()
        provider = Provider(
            [
                LlmResponse("", (LlmToolCall("read", "read_file", '{"path":"evidence"}'),)),
                LlmResponse("", (LlmToolCall("finish", "finish", '{"report":"Verified changes"}'),)),
            ]
        )
        execution = Execution()
        tools = make_task_tools(TaskRequest("Summarize changes", workspace=tmp_path))
        first = await run_managed(tmp_path, provider, execution, tools, planning)
        assert first.ok and first.value.control.kind == "continue"
        assert planning.route.state.phase.value == "reviewing"
        # The service commits this completed step before starting the next one.
        execution.checkpoint = None
        second = await run_managed(tmp_path, provider, execution, tools, planning, context=first.value.context)
        assert second.ok and second.value.control.kind == "completed"
        assert second.value.output == "Verified changes"
        assert "Already up to date" in str(provider.calls[-1])
        assert planning.route.state.phase.value == "completed" and len(provider.calls) == 2

    asyncio.run(scenario())


def test_terminal_checkpoint_honors_late_pause_then_reuses_final_response(tmp_path):
    async def scenario():
        class LatePause(Execution):
            pause = True

            def boundary(self, cp):
                directive = super().boundary(cp)
                if cp["phase"] == "step_done" and self.pause:
                    directive["control"] = {"kind": "paused", "reason": "late pause"}
                return directive

        execution = LatePause()
        provider = Provider([final("cached")])
        first = await run_managed(tmp_path, provider, execution)
        assert first.ok and first.value.control.kind == "paused"
        execution.pause = False
        resumed = await run_managed(tmp_path, provider, execution)
        assert resumed.ok and resumed.value.control.kind == "completed"
        assert len(provider.calls) == 1

    asyncio.run(scenario())


def test_request_reservation_honors_pause_before_calling_model(tmp_path):
    async def scenario():
        class PauseReservation(Execution):
            def boundary(self, cp):
                directive = super().boundary(cp)
                if cp["llm_calls"]:
                    directive["control"] = {"kind": "paused", "reason": "reserved"}
                return directive

        provider = Provider([final()])
        result = await run_managed(tmp_path, provider, PauseReservation())
        assert result.ok and result.value.control.kind == "paused"
        assert provider.calls == []

    asyncio.run(scenario())


def test_pause_at_question_checkpoint_preserves_pending_request(tmp_path):
    async def scenario():
        class PauseQuestion(Execution):
            pause = True

            def boundary(self, cp):
                directive = super().boundary(cp)
                if cp.get("pending_input") and self.pause:
                    directive["control"] = {"kind": "paused"}
                return directive

        provider = Provider([LlmResponse("", (LlmToolCall("ask", "request_input", '{"question":"Which?"}'),)), final()])
        execution = PauseQuestion()
        first = await run_managed(tmp_path, provider, execution)
        assert first.value.control.kind == "paused"
        assert execution.checkpoint["pending_input"]["request_id"] == "question-1"
        execution.pause = False
        execution.answer = {"request_id": "question-1", "answer": "Payments"}
        resumed = await run_managed(tmp_path, provider, execution)
        assert resumed.value.control.kind == "completed"
        assert execution.operations == ["ask"]

    asyncio.run(scenario())


def test_resume_exhausted_workflow_response_requests_model_again_and_preserves_usage(tmp_path):
    async def scenario():
        execution = Execution()
        invalid = LlmResponse(final("old answer").content, usage=TokenUsage(2, 1, 3))
        failed = await run_managed(tmp_path, Provider([invalid, invalid]), execution, planning=PlanningRuntime("auto"))
        assert not failed.ok and failed.error.code == "WORKFLOW_ROUTE_FAILED"
        assert execution.checkpoint["phase"] == "after_llm"
        assert execution.checkpoint["missing_retries"] == 1
        execution.inputs = [{"seq": 10, "content": "Now run the query sample"}]
        provider = Provider([LlmResponse("", (LlmToolCall("route", "continue_react", '{"reason":"Run query"}'),))])
        resumed = await run_managed(tmp_path, provider, execution, planning=PlanningRuntime("auto"))
        assert resumed.ok and resumed.value.control.kind == "continue"
        assert len(provider.calls) == 1
        assert any(message.role == "user" and "Now run the query sample" in message.content for message in provider.calls[0])
        assert execution.checkpoint["llm_calls"] == 3
        assert execution.checkpoint["usage"].total_tokens == 6
        assert execution.operations == ["route"]

    asyncio.run(scenario())


def test_resume_keeps_valid_route_response_after_a_missing_tool_retry(tmp_path):
    async def scenario():
        class PauseValidResponse(Execution):
            pause = True

            def boundary(self, checkpoint):
                directive = super().boundary(checkpoint)
                if checkpoint["phase"] == "after_llm" and checkpoint["response"].tool_calls and self.pause:
                    directive["control"] = {"kind": "paused"}
                return directive

        execution = PauseValidResponse()
        provider = Provider([final(), LlmResponse("", (LlmToolCall("route", "continue_react", '{"reason":"Direct"}'),))])
        paused = await run_managed(tmp_path, provider, execution, planning=PlanningRuntime("auto"))
        assert paused.ok and paused.value.control.kind == "paused"
        assert execution.checkpoint["missing_retries"] == 1
        execution.pause = False
        resumed = await run_managed(tmp_path, provider, execution, planning=PlanningRuntime("auto"))
        assert resumed.ok and resumed.value.control.kind == "continue"
        assert len(provider.calls) == 2
        assert execution.operations == ["route"]

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [False, True])
def test_progress_review_rejects_task_call_and_explains_required_action(tmp_path, native):
    async def scenario():
        planning = PlanningRuntime("auto")
        planning.route.select_react("Inspect changes").unwrap()
        planning.route.mark_task_execution_started()
        planning.request_route_review("tool_budget", "12 task tool calls").unwrap()
        invalid = (
            LlmResponse("", (LlmToolCall("wrong", "shell_execute", '{"command":"touch wrong"}'),))
            if native
            else LlmResponse(json.dumps({"reasoning": "Missing commit history", "action": {
                "kind": "tool", "target": "shell_execute", "input": {"command": "touch wrong"},
            }}))
        )
        selected = LlmResponse(json.dumps({"reasoning": "One history query remains", "action": {
            "kind": "tool", "target": "continue_react", "input": {
                "reason": "A read-only query closes the gap", "evidence_gap": "Three commits unidentified", "next_action": "Read full git log",
            },
        }}))
        provider = Provider([invalid, selected])
        execution = Execution()
        result = await run_managed(tmp_path, provider, execution, planning=planning)
        assert result.ok and result.value.control.kind == "continue"
        assert planning.route.state.phase.value == "react"
        assert len(execution.operations) == 1
        assert not (tmp_path / "wrong").exists()
        feedback = provider.calls[1][-1].content
        assert "shell_execute is unavailable" in feedback
        assert '"target":"continue_react"' in feedback
        assert '"input":{' in feedback
        assert "Three commits unidentified" in planning.route.state.reason
        if native:
            assert provider.calls[1][-2].tool_call_id == "wrong"
            assert "unavailable" in provider.calls[1][-2].content

    asyncio.run(scenario())


def test_yakdb_progress_review_failure_is_bounded_and_resume_corrects_field_only_response(tmp_path):
    async def scenario():
        planning = PlanningRuntime("auto")
        planning.route.select_react("Inspect changes").unwrap()
        planning.route.mark_task_execution_started()
        planning.request_route_review("tool_budget", "12 task tool calls").unwrap()
        execution = Execution()
        wrong_action = LlmResponse(json.dumps({"reasoning": "One cheap read closes the gap", "action": {
            "kind": "tool", "target": "shell_execute", "input": {"command": "git log"},
        }}), usage=TokenUsage(2, 1, 3))
        fields_only = LlmResponse(json.dumps({
            "reasoning": "Update verified, history incomplete", "evidence_gap": "Three commits unidentified", "next_action": "Read full git log",
        }), usage=TokenUsage(2, 1, 3))
        provider = Provider([wrong_action, fields_only])
        failed = await run_managed(tmp_path, provider, execution, planning=planning)
        assert not failed.ok and failed.error.code == "WORKFLOW_ROUTE_FAILED"
        assert len(provider.calls) == 2 and execution.operations == []
        assert tuple(failed.error.cause["available_tools"]) == ("enter_plan", "continue_react", "finish")
        selected = LlmResponse("", (LlmToolCall("route", "continue_react", json.dumps({
            "reason": "Read-only lookup", "evidence_gap": "Three commits unidentified", "next_action": "Read full git log",
        })),))
        retry_provider = Provider([selected])
        resumed = await run_managed(tmp_path, retry_provider, execution, planning=PlanningRuntime("auto"))
        assert resumed.ok and resumed.value.control.kind == "continue"
        assert "No executable tool call was found" in retry_provider.calls[0][-1].content
        assert execution.checkpoint["llm_calls"] == 3
        assert execution.checkpoint["usage"].total_tokens == 6
        assert execution.operations == ["route"]

    asyncio.run(scenario())
