import asyncio
import json
from pathlib import Path

import pytest

from loom.core import (
    Action,
    Context,
    Decision,
    GoalLayer,
    IdentityLayer,
    KnowledgeItem,
    Observation,
    StateLayer,
    ToolRef,
    empty_affordances,
    empty_knowledge,
    freeze_context,
    new_context_id,
    new_loop_id,
    new_run_id,
    ok,
)
from loom.llm import (
    LlmMessage,
    LlmResponse,
    LlmStreamEvent,
    LlmToolCall,
    TokenUsage,
    build_messages,
    build_system_prompt,
    build_user_prompt,
    create_env_openai_provider,
    create_llm_step_function,
    create_openai_provider,
    create_token_tracker,
    load_env_openai_config,
    to_llm_tool,
    to_llm_tools,
)

NOW = "2026-06-04T00:00:00.000Z"


def test_prompt_builder_tools_and_token_tracker():
    context = make_context()

    system = build_system_prompt(context)
    assert "research planner" in system
    assert "MUST: Return machine-readable decisions" in system
    assert "search" in system
    assert "confidence" in system

    user = build_user_prompt(context, max_history_steps=1)
    assert "Step number: 1" in user
    assert '"status": "ready"' in user
    assert "Need external context" in user
    assert "The index contains project notes." in user

    messages = build_messages(context)
    assert [message.role for message in messages] == ["system", "user"]

    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    tool = ToolRef("search", "Search indexed notes", input_schema=schema)
    assert to_llm_tool(tool)["function"]["parameters"] == schema
    assert to_llm_tools((tool,))[0]["function"]["name"] == "search"
    assert to_llm_tool(ToolRef("clock", "Read time"))["function"]["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }

    tracker = create_token_tracker()
    tracker.add(TokenUsage(10, 5, 15))
    tracker.add(TokenUsage(2, 3, 5))
    assert tracker.total == TokenUsage(12, 8, 20)
    assert tracker.is_within_budget(20)
    assert not tracker.is_within_budget(19)
    tracker.reset()
    assert tracker.total.total_tokens == 0


def test_llm_step_structured_tool_calls_fallback_and_budget():
    async def scenario():
        structured_provider = FakeProvider(
            [
                ok(
                    LlmResponse(
                        content=json.dumps(
                            {
                                "reasoning": "No tool needed",
                                "action": {
                                    "kind": "none",
                                    "description": "Wait for more information",
                                },
                                "alternatives": [{"kind": "custom", "description": "Ask for clarification"}],
                                "confidence": 0.9,
                            }
                        ),
                        usage=TokenUsage(10, 5, 15),
                    )
                )
            ]
        )
        step_fn = create_llm_step_function(structured_provider, enable_tool_calling=False)
        result = await step_fn(make_context(), make_runtime())
        assert result.ok
        assert len(result.value.context.state.observations) == 2
        assert result.value.context.state.decisions[-1].reasoning == "No tool needed"
        assert result.value.trace.actions[0].kind == "none"
        assert result.value.trace.metadata["model"] == "mock-model"

        tool_provider = FakeProvider(
            [
                ok(
                    LlmResponse(
                        content=None,
                        tool_calls=(LlmToolCall("call_1", "search", '{"query":"loom"}'),),
                        finish_reason="tool_calls",
                    )
                ),
                ok(
                    LlmResponse(
                        content=json.dumps(
                            {
                                "reasoning": "Tool result is enough",
                                "action": {
                                    "kind": "tool",
                                    "target": "search",
                                    "description": "Use the search result",
                                    "input": {"query": "loom"},
                                },
                                "alternatives": [],
                                "confidence": 0.8,
                            }
                        )
                    )
                ),
            ]
        )
        tool_calls = []

        async def call_tool(name, input_value, **options):
            tool_calls.append((name, input_value, options))
            return ok(Observation("search-obs", "search", {"result": "found"}, NOW))

        result = await create_llm_step_function(tool_provider)(make_context(), make_runtime(call_tool=call_tool))
        assert result.ok
        assert tool_calls[0][0] == "search"
        assert tool_calls[0][1] == {"query": "loom"}
        assert tool_calls[0][2]["metadata"]["tool_call_id"] == "call_1"
        assert len(result.value.context.state.observations) == 3

        fallback = await create_llm_step_function(
            FakeProvider([ok(LlmResponse(content="plain text decision"))]),
            enable_tool_calling=False,
        )(make_context(), make_runtime())
        assert fallback.ok
        assert fallback.value.context.state.decisions[-1].action.kind == "custom"

        fenced_json = await create_llm_step_function(
            FakeProvider(
                [
                    ok(
                        LlmResponse(
                            content="""```json
{
  "reasoning": "The tool result is enough",
  "action": {
    "kind": "tool",
    "target": "search",
    "description": "Use the search result",
    "input": {"query": "loom"}
  },
  "alternatives": [],
  "confidence": 0.8
}
```"""
                        )
                    )
                ]
            ),
            enable_tool_calling=False,
        )(make_context(), make_runtime())
        assert fenced_json.ok
        fenced_decision = fenced_json.value.context.state.decisions[-1]
        assert fenced_decision.metadata["parseFallback"] is False
        assert fenced_decision.action.target == "search"

        budget = await create_llm_step_function(
            FakeProvider(
                [
                    ok(
                        LlmResponse(
                            content='{"reasoning":"too expensive","action":{"kind":"none","description":"Stop"},"alternatives":[],"confidence":0.1}',
                            usage=TokenUsage(6, 5, 11),
                        )
                    )
                ]
            ),
            enable_tool_calling=False,
        )(make_context(max_tokens=10), make_runtime())
        assert not budget.ok
        assert budget.error.code == "TOKEN_BUDGET_EXCEEDED"

        invalid = await create_llm_step_function(
            FakeProvider(
                [
                    ok(
                        LlmResponse(
                            content=None,
                            tool_calls=(LlmToolCall("call_1", "search", "{invalid"),),
                        )
                    )
                ]
            )
        )(make_context(), make_runtime())
        assert not invalid.ok
        assert invalid.error.code == "LLM_PARSE_ERROR"

    asyncio.run(scenario())


def test_openai_provider_request_parsing_and_error_mapping():
    async def scenario():
        calls = []

        async def http_client(url, request):
            calls.append((url, request))
            return {
                "status": 200,
                "ok": True,
                "json": {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "search",
                                            "arguments": '{"query":"loom"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    },
                },
            }

        provider = create_openai_provider(
            api_key="test-key",
            model="gpt-test",
            temperature=0.2,
            max_tokens=256,
            base_url="https://proxy.example/v1/",
            http_client=http_client,
        )
        result = await provider.chat([{"role": "user", "content": "hello"}])

        assert result.ok
        assert calls[0][0] == "https://proxy.example/v1/chat/completions"
        assert calls[0][1]["headers"]["Authorization"] == "Bearer test-key"
        assert calls[0][1]["body"]["model"] == "gpt-test"
        assert result.value.tool_calls[0].name == "search"
        assert result.value.usage.total_tokens == 18

        async def error_client(_url, _request):
            return {
                "status": 429,
                "ok": False,
                "json": {"error": {"message": "Too many requests"}},
            }

        failed = await create_openai_provider(api_key="key", model="gpt-test", http_client=error_client).chat([{"role": "user", "content": "hello"}])
        assert not failed.ok
        assert failed.error.code == "LLM_FAILED"
        assert failed.error.retryable is True

    asyncio.run(scenario())


def test_env_config_loads_openai_compatible_provider(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "LOOM_LLM_MODEL=qwen3.6-max-preview",
                "LOOM_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1",
                "LOOM_LLM_API_KEY=test-loom-key",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_env_openai_config(env_path=env_path, env={})

    assert config.ok
    assert config.value.model == "qwen3.6-max-preview"
    assert config.value.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.value.api_key == "test-loom-key"

    async def scenario():
        calls = []

        async def http_client(url, request):
            calls.append((url, request))
            return {
                "status": 200,
                "ok": True,
                "json": {
                    "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                    "usage": {},
                },
            }

        provider = create_env_openai_provider(env_path=env_path, env={}, http_client=http_client)
        assert provider.ok

        result = await provider.value.chat([{"role": "user", "content": "hello"}])

        assert result.ok
        assert calls[0][0] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        assert calls[0][1]["headers"]["Authorization"] == "Bearer test-loom-key"
        assert calls[0][1]["body"]["model"] == "qwen3.6-max-preview"

    asyncio.run(scenario())


def test_openai_provider_stream_chat_parses_sse_chunks():
    async def scenario():
        calls = []
        first_chunk = {"choices": [{"delta": {"content": '{"reasoning":"ok",'}, "finish_reason": None}]}
        second_chunk = {
            "choices": [
                {
                    "delta": {
                        "content": '"action":{"kind":"none","description":"Stop"},"alternatives":[],"confidence":0.7}',
                    },
                    "finish_reason": None,
                }
            ]
        }
        usage_chunk = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }

        async def http_client(url, request):
            calls.append((url, request))
            return {
                "status": 200,
                "ok": True,
                "chunks": [
                    f"data: {json.dumps(first_chunk)}\n\n",
                    f"data: {json.dumps(second_chunk)}\n\n",
                    f"data: {json.dumps(usage_chunk)}\n\n",
                    "data: [DONE]\n\n",
                ],
            }

        provider = create_openai_provider(
            api_key="test-key",
            model="gpt-test",
            base_url="https://proxy.example/v1",
            http_client=http_client,
        )

        events = [event async for event in provider.stream_chat([LlmMessage("user", "hello")])]

        assert calls[0][0] == "https://proxy.example/v1/chat/completions"
        assert calls[0][1]["body"]["stream"] is True
        assert [event.kind for event in events] == ["content.delta", "content.delta", "completed"]
        assert events[-1].response.content.startswith('{"reasoning":"ok"')
        assert events[-1].response.usage.total_tokens == 7

    asyncio.run(scenario())


def test_default_env_config_loads_project_dotenv_when_present():
    env_path = Path(".env")
    if not env_path.exists():
        pytest.skip("project .env is local-only and ignored by git")

    config = load_env_openai_config(env={})

    assert config.ok
    assert config.value.env_path == ".env"
    assert config.value.model
    assert config.value.base_url.startswith("http")
    assert config.value.api_key


def test_llm_step_emits_full_provider_io_events():
    async def scenario():
        events = []
        provider = FakeProvider(
            [
                ok(
                    LlmResponse(
                        content=None,
                        tool_calls=(LlmToolCall("call_1", "search", '{"query":"loom"}'),),
                        usage=TokenUsage(11, 2, 13),
                        finish_reason="tool_calls",
                    )
                ),
                ok(
                    LlmResponse(
                        content=json.dumps(
                            {
                                "reasoning": "Tool result is enough",
                                "action": {
                                    "kind": "tool",
                                    "target": "search",
                                    "description": "Use the search result",
                                    "input": {"query": "loom"},
                                },
                                "alternatives": [],
                                "confidence": 0.8,
                            }
                        ),
                        usage=TokenUsage(17, 5, 22),
                    )
                ),
            ]
        )

        async def call_tool(_name, _input_value, **_options):
            return ok(Observation("search-obs", "search", {"result": "found"}, NOW))

        result = await create_llm_step_function(provider)(make_context(), make_runtime(call_tool=call_tool, events=events))

        assert result.ok
        assert [event["type"] for event in events] == [
            "llm.requested",
            "llm.completed",
            "llm.requested",
            "llm.completed",
            "decision.recorded",
            "action.started",
            "observation.recorded",
            "observation.recorded",
            "action.completed",
        ]
        first_request = events[0]
        first_response = events[1]
        second_request = events[2]
        assert first_request["model"] == "mock-model"
        assert [message.role for message in first_request["messages"]] == ["system", "user"]
        assert first_request["tools"][0]["function"]["name"] == "search"
        assert first_response["response"].tool_calls[0].name == "search"
        assert first_request["llm_call_id"] == first_response["llm_call_id"]
        assert [message.role for message in second_request["messages"]] == ["system", "user", "assistant", "tool"]
        assert events[3]["response"].usage.total_tokens == 22
        assert "api_key" not in json.dumps(str(events))

    asyncio.run(scenario())


def test_llm_step_streaming_provider_emits_delta_events_and_executes_tool_calls():
    async def scenario():
        events = []
        final_content = json.dumps(
            {
                "reasoning": "Tool result is enough",
                "action": {
                    "kind": "tool",
                    "target": "search",
                    "description": "Use the search result",
                    "input": {"query": "loom"},
                },
                "alternatives": [],
                "confidence": 0.8,
            },
            separators=(",", ":"),
        )
        split_at = final_content.index('"action"')
        provider = FakeStreamingProvider(
            [
                [
                    LlmStreamEvent(kind="tool_call.started", tool_call_id="call_1", tool_name="search"),
                    LlmStreamEvent(kind="tool_call.arguments.delta", tool_call_id="call_1", tool_arguments_delta='{"query":"loom"}'),
                    LlmStreamEvent(
                        kind="tool_call.completed",
                        tool_call_id="call_1",
                        tool_name="search",
                        tool_arguments_delta='{"query":"loom"}',
                    ),
                    LlmStreamEvent(
                        kind="completed",
                        response=LlmResponse(
                            content=None,
                            tool_calls=(LlmToolCall("call_1", "search", '{"query":"loom"}'),),
                            finish_reason="tool_calls",
                        ),
                    ),
                ],
                [
                    LlmStreamEvent(kind="reasoning.delta", reasoning_delta="Evidence is enough."),
                    LlmStreamEvent(kind="content.delta", content_delta=final_content[:split_at]),
                    LlmStreamEvent(
                        kind="content.delta",
                        content_delta=final_content[split_at:],
                    ),
                    LlmStreamEvent(
                        kind="completed",
                        response=LlmResponse(
                            content=final_content,
                            usage=TokenUsage(12, 6, 18),
                        ),
                    ),
                ],
            ]
        )
        tool_calls = []

        async def call_tool(name, input_value, **options):
            tool_calls.append((name, input_value, options))
            return ok(Observation("search-obs", "search", {"result": "found"}, NOW))

        result = await create_llm_step_function(provider, stream=True)(make_context(), make_runtime(call_tool=call_tool, events=events))

        assert result.ok
        assert tool_calls[0][0] == "search"
        event_types = [event["type"] for event in events]
        assert "llm.stream.started" in event_types
        assert "llm.tool_call.started" in event_types
        assert "llm.tool_call.arguments.delta" in event_types
        assert "llm.tool_call.completed" in event_types
        assert "llm.reasoning.delta" in event_types
        assert "llm.content.delta" in event_types
        assert event_types.count("llm.stream.completed") == 2
        assert events[event_types.index("llm.content.delta")]["delta"]
        assert result.value.trace.metadata["streaming"] is True

    asyncio.run(scenario())


class FakeProvider:
    model = "mock-model"

    def __init__(self, results):
        self.results = list(results)
        self.messages = []

    async def chat(self, messages, tools=None, cancellation=None):
        self.messages.append((tuple(messages), tools))
        if self.results:
            return self.results.pop(0)
        return ok(LlmResponse(content='{"reasoning":"default","action":{"kind":"none","description":"Stop"},"alternatives":[],"confidence":0.1}'))


class FakeStreamingProvider(FakeProvider):
    def __init__(self, streams):
        super().__init__([])
        self.streams = list(streams)

    async def stream_chat(self, messages, tools=None, cancellation=None):
        self.messages.append((tuple(messages), tools))
        for event in self.streams.pop(0):
            yield event


def make_context(max_tokens=None):
    observation = Observation("obs-1", "sensor", {"status": "ready"}, NOW)
    action = Action("action-1", "tool", "Search the index", target="search", input={"query": "loom"})
    decision = Decision("decision-1", action, "Need external context", (), 0.75, NOW)
    fact = KnowledgeItem("fact-1", "fact", "The index contains project notes.", 0.9, NOW)
    heuristic = KnowledgeItem("heuristic-1", "heuristic", "Prefer reversible steps.", 0.8, NOW)
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=NOW,
            identity=IdentityLayer(
                role="research planner",
                capabilities=(ToolRef("plan", "Create next-step plans"),),
                constraints=(
                    {
                        "id": "json-only",
                        "description": "Return machine-readable decisions",
                        "severity": "must",
                    },
                ),
            ),
            goal=GoalLayer(
                objective="Find the next useful action",
                budget={} if max_tokens is None else {"max_tokens": max_tokens},
            ),
            state=StateLayer(observations=(observation,), decisions=(decision,)),
            knowledge=empty_knowledge(facts=(fact,), heuristics=(heuristic,)),
            affordances=empty_affordances(tools=(ToolRef("search", "Search indexed notes", input_schema={"type": "object"}),)),
        )
    )


def make_runtime(call_tool=None, events=None):
    async def default_call_tool(_name, _input_value, **_options):
        return ok(Observation("tool-obs", "search", {"result": "found"}, NOW))

    async def emit(event):
        if events is not None:
            events.append(event)
        return ok(None)

    return type(
        "Runtime",
        (),
        {
            "run_id": new_run_id(),
            "loop_id": new_loop_id(),
            "cancellation": None,
            "registry": None,
            "trace_sink": type("Sink", (), {"emit": staticmethod(emit)})(),
            "now": staticmethod(lambda: NOW),
            "call_tool": staticmethod(call_tool or default_call_tool),
        },
    )()
