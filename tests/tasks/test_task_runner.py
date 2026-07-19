import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from loom.llm import LlmResponse, LlmStreamEvent, LlmToolCall, TokenUsage
from loom.tasks.profiles import select_task_profile
from loom.tasks.request import TaskHarness, TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task


def test_make_task_context_maps_request_to_loom_layers(tmp_path):
    request = TaskRequest(
        objective="Audit this project",
        workspace=tmp_path,
        profile="project_audit",
        constraints=("Do not modify source files.",),
        expected_outputs=("markdown report",),
    )

    context = make_task_context(request).unwrap()

    assert context.goal.objective == "Audit this project"
    assert context.goal.criteria[0].description == "markdown report"
    assert context.identity.role == "project audit task runner"
    assert any("Do not modify source files." in item.description for item in context.identity.constraints)
    assert context.metadata["profile"] == "project_audit"
    assert context.metadata["workspace"] == str(tmp_path)
    assert {tool.id for tool in context.affordances.tools} >= {
        "read_file",
        "edit_file",
        "write_file",
        "shell_execute",
        "finish",
    }


def test_make_task_context_exposes_exact_edit_file_schema(tmp_path):
    context = make_task_context(TaskRequest("Edit this project", workspace=tmp_path)).unwrap()
    tool = next(item for item in context.affordances.tools if item.id == "edit_file")
    schema = tool.input_schema

    assert schema["required"] == ("path", "edits")
    assert schema["additionalProperties"] is False
    item_schema = schema["properties"]["edits"]["items"]
    assert item_schema["required"] == ("old_text", "new_text")
    assert item_schema["properties"]["occurrence"]["minimum"] == 1
    assert item_schema["additionalProperties"] is False


def test_auto_profile_selects_project_audit_for_workspace_audit(tmp_path):
    request = TaskRequest("Audit this project and suggest improvements", workspace=tmp_path)

    profile = select_task_profile(request)

    assert profile.id == "project_audit"


def test_make_task_context_rejects_missing_workspace(tmp_path):
    request = TaskRequest("Audit this project", workspace=tmp_path / "missing", profile="project_audit")

    result = make_task_context(request)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_task_harness_limits_tools_and_adds_system_prompt_constraint(tmp_path):
    harness = TaskHarness(
        system_prompt_addendum="Always cite the exact file path.",
        allowed_tools=("read_file",),
        max_tool_calls_per_step=4,
        max_history_steps=2,
    )

    context = make_task_context(TaskRequest("Audit", workspace=tmp_path), harness=harness).unwrap()

    assert tuple(tool.id for tool in context.affordances.tools) == ("read_file",)
    assert any("Always cite the exact file path." in item.description for item in context.identity.constraints)


def test_task_harness_rejects_unknown_allowed_tool(tmp_path):
    result = make_task_context(
        TaskRequest("Audit", workspace=tmp_path),
        harness=TaskHarness(allowed_tools=("not_a_real_tool",)),
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


class FakeTaskProvider:
    model = "fake-task-model"

    def __init__(self) -> None:
        self.calls = 0
        self.messages_seen = []

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls += 1
        self.messages_seen.append(tuple(messages))
        if self.calls == 1:
            return self._tool_response()
        return self._final_response()

    def _tool_response(self):
        return _response(
            content="",
            tool_calls=(
                LlmToolCall("call-read", "read_file", json.dumps({"path": "README.md"})),
                LlmToolCall("call-finish", "finish", json.dumps({"report": "# Demo audit\n\nLooks healthy."})),
            ),
            finish_reason="tool_calls",
        )

    def _final_response(self):
        return _response(
            content=json.dumps(
                {
                    "reasoning": "The README was inspected and finish captured the report.",
                    "action": {
                        "kind": "none",
                        "description": "task complete",
                        "target": None,
                        "input": {},
                    },
                    "alternatives": [],
                    "confidence": 0.9,
                }
            )
        )


class FakeEditTaskProvider:
    model = "fake-edit-task-model"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls += 1
        if self.calls == 1:
            return _response(
                content="",
                tool_calls=(
                    LlmToolCall(
                        "call-edit",
                        "edit_file",
                        json.dumps(
                            {
                                "path": "sample.txt",
                                "edits": [
                                    {
                                        "old_text": "same",
                                        "new_text": "changed",
                                        "occurrence": 2,
                                    }
                                ],
                            }
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            return _response(
                content="",
                tool_calls=(
                    LlmToolCall(
                        "call-finish",
                        "finish",
                        json.dumps({"report": "Edited sample."}),
                    ),
                ),
                finish_reason="tool_calls",
            )
        return _response(
            content=json.dumps(
                {
                    "reasoning": "The requested occurrence was edited and reported.",
                    "action": {
                        "kind": "none",
                        "description": "task complete",
                        "target": None,
                        "input": {},
                    },
                    "alternatives": [],
                    "confidence": 0.9,
                }
            )
        )


class StreamingTaskProvider:
    model = "fake-streaming-task-model"

    async def stream_chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        content = json.dumps(
            {
                "reasoning": "The project was audited briefly.",
                "action": {
                    "kind": "none",
                    "description": "task complete",
                    "target": None,
                    "input": {},
                },
                "alternatives": [],
                "confidence": 0.82,
            }
        )
        split_at = content.index('"action"')
        yield LlmStreamEvent(kind="content.delta", content_delta=content[:split_at])
        yield LlmStreamEvent(kind="content.delta", content_delta=content[split_at:])
        yield LlmStreamEvent(kind="completed", response=LlmResponse(content=content, usage=TokenUsage(4, 5, 9)))


@dataclass(frozen=True)
class OverlayRecordingProvider:
    api_key: str = "secret-key"
    model: str = "fixed-model"
    base_url: str = "https://provider.invalid/v1"
    request_options: dict[str, Any] = field(default_factory=lambda: {"enable_thinking": False})
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls.append(
            {
                "api_key": self.api_key,
                "model": self.model,
                "base_url": self.base_url,
                "request_options": self.request_options,
                "tool_names": tuple(tool["function"]["name"] for tool in tools or ()),
            }
        )
        return _response(
            content=json.dumps(
                {
                    "reasoning": "The bounded harness was applied.",
                    "action": {"kind": "none", "description": "done", "target": None, "input": {}},
                    "alternatives": [],
                    "confidence": 0.9,
                }
            )
        )


def _response(*, content, tool_calls=(), finish_reason="stop"):
    from loom.core import ok

    return ok(LlmResponse(content=content, tool_calls=tool_calls, usage=TokenUsage(1, 2, 3), finish_reason=finish_reason))


def test_run_generic_task_executes_llm_tool_loop_and_returns_finish_report(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nA demo project.\n", encoding="utf-8")
    provider = FakeTaskProvider()

    result = asyncio.run(run_generic_task(TaskRequest("Audit this project", workspace=tmp_path, profile="project_audit"), provider=provider))

    assert result.ok
    assert "Demo audit" in result.value.output
    assert provider.calls >= 2
    assert any(message.role == "tool" for message in provider.messages_seen[-1])


def test_run_generic_task_executes_edit_file_and_traces_observation(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    trace_path = tmp_path / "runs" / "edit-task.jsonl"

    result = asyncio.run(
        run_generic_task(
            TaskRequest("Edit the second repeated string", workspace=tmp_path),
            provider=FakeEditTaskProvider(),
            options=TaskRunOptions(trace_path=trace_path),
        )
    )

    assert result.ok
    assert target.read_text(encoding="utf-8") == "same\nchanged\n"
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    completed = [record for record in records if record.get("eventType") == "tool.completed"]
    edit_record = next(record for record in completed if record["payload"]["tool_id"] == "edit_file")
    assert edit_record["payload"]["input"]["edits"][0]["occurrence"] == 2
    assert edit_record["payload"]["output"]["value"]["replacements"] == 1


def test_run_generic_task_trace_omits_stream_token_deltas(tmp_path):
    trace_path = tmp_path / "runs" / "sample-task.jsonl"

    result = asyncio.run(
        run_generic_task(
            TaskRequest("Audit this project briefly", workspace=tmp_path, profile="project_audit"),
            provider=StreamingTaskProvider(),
            options=TaskRunOptions(stream=True, trace_path=trace_path),
        )
    )

    assert result.ok
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_types = [record["eventType"] for record in records if record["type"] == "event"]
    assert "llm.requested" in event_types
    assert "llm.completed" in event_types
    assert "llm.stream.completed" in event_types
    assert "llm.content.delta" not in event_types


def test_run_generic_task_merges_harness_request_options_without_changing_provider_identity(tmp_path):
    provider = OverlayRecordingProvider()

    result = asyncio.run(
        run_generic_task(
            TaskRequest("Audit", workspace=tmp_path),
            provider=provider,
            harness=TaskHarness(
                allowed_tools=("read_file",),
                request_options={"enable_thinking": True, "thinking_budget": 1024},
            ),
        )
    )

    assert result.ok
    assert provider.request_options == {"enable_thinking": False}
    assert provider.calls == [
        {
            "api_key": "secret-key",
            "model": "fixed-model",
            "base_url": "https://provider.invalid/v1",
            "request_options": {"enable_thinking": True, "thinking_budget": 1024},
            "tool_names": ("read_file",),
        }
    ]
