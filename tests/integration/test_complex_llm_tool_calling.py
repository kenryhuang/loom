"""
Complex project-level test: LLM tool-calling with error recovery and budget.

Tests realistic LLM integration scenarios:
1. Multi-turn tool calling with tool result feedback loop
2. Token budget enforcement (stops when budget exceeded)
3. Max tool calls per step enforcement
4. LLM provider error handling (retryable vs non-retryable)
5. Prompt builder with full context (history + knowledge + tools)
6. Decision parsing with malformed LLM responses

Exercises: llm, runtime, core models, observability.
"""

from __future__ import annotations

import json

import pytest

from loom.core import (
    Context,
    Decision,
    GoalLayer,
    IdentityLayer,
    KnowledgeItem,
    MinimalLoopDefinition,
    Observation,
    StateLayer,
    ToolRef,
    empty_affordances,
    empty_knowledge,
    empty_state,
    freeze_context,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    now_iso,
    ok,
)
from loom.llm import (
    TokenUsage,
    build_messages,
    build_system_prompt,
    build_user_prompt,
    create_llm_step_function,
    create_openai_provider,
    create_token_tracker,
    to_llm_tool,
)
from loom.runtime import (
    create,
    create_runtime_registry,
    run,
)


def _make_llm_context(
    *,
    max_steps: int = 3,
    max_tokens: int | None = None,
    tools: tuple[ToolRef, ...] = (),
    knowledge_facts: tuple[KnowledgeItem, ...] = (),
    observations: tuple[Observation, ...] = (),
    decisions: tuple[Decision, ...] = (),
):
    """Create a context configured for LLM loop testing."""
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(
                role="test agent",
                capabilities=(),
                constraints=(),
            ),
            goal=GoalLayer(
                objective="Test LLM integration",
                budget={"max_steps": max_steps, "max_tokens": max_tokens},
            ),
            state=StateLayer(observations=observations, decisions=decisions),
            knowledge=empty_knowledge(facts=knowledge_facts),
            affordances=empty_affordances(tools=tools),
        )
    )


def _make_mock_http_client(responses: list[dict]):
    """Create a mock HTTP client that returns predefined LLM responses."""
    call_index = [0]

    async def http_client(url, request):
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(responses):
            return responses[idx]
        return {
            "status": 200,
            "ok": True,
            "json": {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"reasoning": "done", "action": {"kind": "none", "description": "stop"}, "alternatives": [], "confidence": 1.0}
                            )
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        }

    return http_client


class TestMultiTurnToolCalling:
    """Test LLM tool-calling with multiple rounds of tool use."""

    @pytest.mark.asyncio
    async def test_two_round_tool_calling(self):
        """LLM calls tool, gets result, calls another tool, then decides."""
        search_tool = ToolRef(
            "search",
            "Search for information",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        write_tool = ToolRef(
            "write",
            "Write a summary",
            input_schema={"type": "object", "properties": {"content": {"type": "string"}}},
        )

        tool_calls_log = []

        async def search_handler(input_value, options):
            tool_calls_log.append(("search", input_value))
            return ok(Observation("search-obs", "search", {"result": "found data"}, now_iso()))

        async def write_handler(input_value, options):
            tool_calls_log.append(("write", input_value))
            return ok(Observation("write-obs", "write", {"status": "written"}, now_iso()))

        # Response sequence: tool_call(search) → tool_call(write) → final decision
        http_client = _make_mock_http_client(
            [
                {
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
                                            "function": {"name": "search", "arguments": json.dumps({"query": "test"})},
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                    },
                },
                {
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
                                            "id": "call_2",
                                            "type": "function",
                                            "function": {"name": "write", "arguments": json.dumps({"content": "summary"})},
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 80, "completion_tokens": 15, "total_tokens": 95},
                    },
                },
                {
                    "status": 200,
                    "ok": True,
                    "json": {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "reasoning": "Both tools executed successfully",
                                            "action": {"kind": "custom", "description": "complete", "input": {}},
                                            "alternatives": [],
                                            "confidence": 0.95,
                                        }
                                    ),
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
                    },
                },
            ]
        )

        provider = create_openai_provider(
            api_key="test-key",
            model="test-model",
            http_client=http_client,
        )
        llm_step = create_llm_step_function(provider, enable_tool_calling=True, max_tool_calls_per_step=5)

        loop_def_obj = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="test"),
            goal=GoalLayer(objective="test"),
            step=llm_step,
            done=lambda ctx, rt: ok(len(ctx.state.decisions) > 0),
        )

        handle = create(
            loop_def_obj,
            registry=create_runtime_registry(tools={"search": search_handler, "write": write_handler}),
        ).unwrap()

        ctx = _make_llm_context(max_steps=1, tools=(search_tool, write_tool))
        result = await run(handle, ctx, max_steps=1)

        assert result.ok
        # Both tools should have been called
        assert len(tool_calls_log) == 2
        assert tool_calls_log[0][0] == "search"
        assert tool_calls_log[1][0] == "write"
        # Final decision recorded
        assert len(result.value.context.state.decisions) == 1
        assert result.value.context.state.decisions[0].confidence == 0.95


class TestTokenBudgetEnforcement:
    """Test that token budgets are enforced during LLM steps."""

    @pytest.mark.asyncio
    async def test_token_budget_exceeded(self):
        """LLM step stops when token budget is exceeded."""
        http_client = _make_mock_http_client(
            [
                {
                    "status": 200,
                    "ok": True,
                    "json": {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "reasoning": "test",
                                            "action": {"kind": "none", "description": "stop"},
                                            "alternatives": [],
                                            "confidence": 1.0,
                                        }
                                    )
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 500, "completion_tokens": 500, "total_tokens": 1000},
                    },
                },
            ]
        )

        provider = create_openai_provider(
            api_key="test-key",
            model="test-model",
            http_client=http_client,
        )
        llm_step = create_llm_step_function(provider, enable_tool_calling=False)

        loop_def_obj = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="test"),
            goal=GoalLayer(objective="test"),
            step=llm_step,
            done=lambda ctx, rt: ok(len(ctx.state.decisions) > 0),
        )

        handle = create(loop_def_obj).unwrap()
        # Budget is 100 tokens, but LLM returns 1000 → budget exceeded
        ctx = _make_llm_context(max_steps=1, max_tokens=100)
        result = await run(handle, ctx, max_steps=1)

        assert not result.ok
        assert result.error.code == "TOKEN_BUDGET_EXCEEDED"


class TestMaxToolCallsEnforcement:
    """Test max tool calls per step limit."""

    @pytest.mark.asyncio
    async def test_max_tool_calls_per_step_exceeded(self):
        """LLM tries to call more tools than allowed per step."""
        tool = ToolRef("tool_a", "A tool", input_schema={"type": "object"})

        async def tool_handler(input_value, options):
            return ok(Observation("tool-obs", "tool_a", {"ok": True}, now_iso()))

        # LLM keeps calling tools — should hit max_tool_calls_per_step limit
        http_client = _make_mock_http_client(
            [
                {
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
                                            "function": {"name": "tool_a", "arguments": "{}"},
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    },
                },
                {
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
                                            "id": "call_2",
                                            "type": "function",
                                            "function": {"name": "tool_a", "arguments": "{}"},
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    },
                },
            ]
        )

        provider = create_openai_provider(
            api_key="test-key",
            model="test-model",
            http_client=http_client,
        )
        # max_tool_calls_per_step=1, but LLM wants to call 2 tools
        llm_step = create_llm_step_function(provider, enable_tool_calling=True, max_tool_calls_per_step=1)

        loop_def_obj = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="test"),
            goal=GoalLayer(objective="test"),
            step=llm_step,
            done=lambda ctx, rt: ok(len(ctx.state.decisions) > 0),
        )

        handle = create(
            loop_def_obj,
            registry=create_runtime_registry(tools={"tool_a": tool_handler}),
        ).unwrap()

        ctx = _make_llm_context(max_steps=1, tools=(tool,))
        result = await run(handle, ctx, max_steps=1)

        # Should fail because max_tool_calls_per_step exceeded
        assert not result.ok


class TestLlmProviderErrorHandling:
    """Test LLM provider error handling."""

    @pytest.mark.asyncio
    async def test_provider_http_error_retryable(self):
        """5xx errors should be marked retryable."""

        async def http_client(url, request):
            return {"status": 503, "ok": False, "json": {"error": {"message": "Service unavailable"}}}

        provider = create_openai_provider(
            api_key="test-key",
            model="test-model",
            http_client=http_client,
        )
        result = await provider.chat(())
        assert not result.ok
        assert result.error.retryable is True
        assert result.error.code == "LLM_FAILED"

    @pytest.mark.asyncio
    async def test_provider_429_rate_limit_retryable(self):
        """429 rate limit should be retryable."""

        async def http_client(url, request):
            return {"status": 429, "ok": False, "json": {"error": {"message": "Rate limit"}}}

        provider = create_openai_provider(
            api_key="test-key",
            model="test-model",
            http_client=http_client,
        )
        result = await provider.chat(())
        assert not result.ok
        assert result.error.retryable is True

    @pytest.mark.asyncio
    async def test_provider_401_not_retryable(self):
        """401 auth error should not be retryable."""

        async def http_client(url, request):
            return {"status": 401, "ok": False, "json": {"error": {"message": "Invalid API key"}}}

        provider = create_openai_provider(
            api_key="test-key",
            model="test-model",
            http_client=http_client,
        )
        result = await provider.chat(())
        assert not result.ok
        assert result.error.retryable is False


class TestPromptBuilder:
    """Test prompt builder with various context configurations."""

    def test_system_prompt_includes_role_and_constraints(self):
        ctx = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(
                    role="code reviewer",
                    constraints=(),
                ),
                goal=GoalLayer(objective="Review code changes"),
                state=empty_state(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(),
            )
        )
        prompt = build_system_prompt(ctx)
        assert "code reviewer" in prompt
        assert "Review code changes" in prompt

    def test_user_prompt_includes_history_and_knowledge(self):
        fact = KnowledgeItem("fact-1", "fact", "Important context", 0.9, now_iso())
        obs = Observation("obs-1", "test", {"data": "value"}, now_iso())
        ctx = _make_llm_context(
            knowledge_facts=(fact,),
            observations=(obs,),
        )
        prompt = build_user_prompt(ctx, include_history=True, include_knowledge=True)
        assert "Important context" in prompt
        assert "Recent observations" in prompt

    def test_user_prompt_excludes_history_when_disabled(self):
        obs = Observation("obs-1", "test", {"data": "value"}, now_iso())
        ctx = _make_llm_context(observations=(obs,))
        prompt = build_user_prompt(ctx, include_history=False, include_knowledge=False)
        assert "Recent observations" not in prompt
        assert "Knowledge facts" not in prompt

    def test_build_messages_returns_system_and_user(self):
        ctx = _make_llm_context()
        messages = build_messages(ctx)
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"


class TestTokenTracker:
    """Test token tracker accumulation and budget checks."""

    def test_accumulate_usage(self):
        tracker = create_token_tracker()
        tracker.add(TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150))
        tracker.add(TokenUsage(prompt_tokens=200, completion_tokens=100, total_tokens=300))
        assert tracker.total.prompt_tokens == 300
        assert tracker.total.completion_tokens == 150
        assert tracker.total.total_tokens == 450

    def test_within_budget(self):
        tracker = create_token_tracker()
        tracker.add(TokenUsage(total_tokens=100))
        assert tracker.is_within_budget(max_tokens=200) is True
        assert tracker.is_within_budget(max_tokens=50) is False
        assert tracker.is_within_budget(max_tokens=None) is True

    def test_reset(self):
        tracker = create_token_tracker()
        tracker.add(TokenUsage(total_tokens=500))
        tracker.reset()
        assert tracker.total.total_tokens == 0


class TestToolSchemaConversion:
    """Test LLM tool schema conversion."""

    def test_tool_with_schema(self):
        tool = ToolRef(
            "search",
            "Search items",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        llm_tool = to_llm_tool(tool)
        assert llm_tool["type"] == "function"
        assert llm_tool["function"]["name"] == "search"
        assert llm_tool["function"]["description"] == "Search items"
        assert "query" in llm_tool["function"]["parameters"]["properties"]

    def test_tool_without_schema_defaults_to_empty_object(self):
        tool = ToolRef("simple", "Simple tool")
        llm_tool = to_llm_tool(tool)
        assert llm_tool["function"]["parameters"]["type"] == "object"
        assert llm_tool["function"]["parameters"]["properties"] == {}
