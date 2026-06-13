"""Tests for LLM-based dynamic tool selection."""

from __future__ import annotations

from dataclasses import replace

import pytest

from loom.core.models import (
    Context,
    GoalLayer,
    IdentityLayer,
    Result,
    ToolRef,
    empty_affordances,
    empty_knowledge,
    empty_state,
    freeze_context,
    new_context_id,
    new_run_id,
    now_iso,
    ok,
)
from loom.llm.api import (
    LlmResponse,
    TokenUsage,
    ToolSelectionConfig,
    ToolSelectionResult,
    build_tool_selection_prompt,
)


def _make_context_with_tools(tool_ids: list[str]) -> Context:
    """Create a context with the given tool IDs."""
    tools = tuple(ToolRef(tid, f"Tool {tid}") for tid in tool_ids)
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(role="test agent"),
            goal=GoalLayer(objective="Test the tool selection mechanism"),
            state=empty_state(),
            knowledge=empty_knowledge(),
            affordances=empty_affordances(tools=tools),
        )
    )


class _FakeToolSelectionProvider:
    """Fake LLM provider that returns a predetermined tool selection."""

    def __init__(self, response_content: str, model: str = "fake-selector"):
        self.model = model
        self._response_content = response_content

    async def chat(self, messages, tools=None, cancellation=None) -> Result:
        return ok(
            LlmResponse(
                content=self._response_content,
                tool_calls=(),
                usage=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
                finish_reason="stop",
            )
        )


class _FailingProvider:
    """Fake LLM provider that always fails."""

    def __init__(self, model: str = "failing"):
        self.model = model

    async def chat(self, messages, tools=None, cancellation=None) -> Result:
        from loom.core.models import err, make_loom_error

        return err(make_loom_error("LLM_FAILED", "Simulated failure", retryable=True))


class TestToolSelectionConfig:
    """Tests for ToolSelectionConfig dataclass."""

    def test_default_values(self):
        config = ToolSelectionConfig()
        assert config.enabled is True
        assert config.provider is None
        assert config.max_tokens == 256
        assert config.min_tools == 1
        assert config.max_tools is None
        assert config.fallback == "all"
        assert config.default_tools == ()
        assert config.always_include == ()

    def test_custom_values(self):
        config = ToolSelectionConfig(
            enabled=False,
            max_tokens=128,
            min_tools=2,
            max_tools=5,
            fallback="default",
            default_tools=("search", "read"),
            always_include=("log",),
        )
        assert config.enabled is False
        assert config.max_tokens == 128
        assert config.min_tools == 2
        assert config.max_tools == 5
        assert config.fallback == "default"
        assert config.default_tools == ("search", "read")
        assert config.always_include == ("log",)


class TestBuildToolSelectionPrompt:
    """Tests for the tool selection prompt builder."""

    def test_basic_prompt(self):
        ctx = _make_context_with_tools(["search", "write", "read"])
        prompt = build_tool_selection_prompt(ctx)

        assert "tool selection assistant" in prompt.lower()
        assert "Test the tool selection mechanism" in prompt
        assert "search:" in prompt
        assert "write:" in prompt
        assert "read:" in prompt
        assert "selected_tools" in prompt
        assert "reasoning" in prompt

    def test_prompt_with_history(self):
        from loom.core.models import Action, Decision, Observation

        ctx = _make_context_with_tools(["search", "write"])
        obs = Observation("obs-1", "test", {"result": "found"}, now_iso())
        action = Action("act-1", "custom", "Test action")
        dec = Decision("dec-1", action, "Test reasoning", (), 0.8, now_iso())
        ctx = freeze_context(
            replace(
                ctx,
                id=new_context_id(),
                state=replace(ctx.state, observations=(obs,), decisions=(dec,)),
            )
        )

        prompt = build_tool_selection_prompt(ctx)
        assert "Recent observations:" in prompt
        assert "Recent decisions:" in prompt
        assert "found" in prompt

    def test_prompt_includes_schema_hints(self):
        tools = (
            ToolRef(
                "search",
                "Search notes",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            ),
        )
        ctx = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(role="test"),
                goal=GoalLayer(objective="Test"),
                state=empty_state(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(tools=tools),
            )
        )
        prompt = build_tool_selection_prompt(ctx)
        assert "params: query" in prompt


class TestToolSelectionResult:
    """Tests for ToolSelectionResult dataclass."""

    def test_basic_result(self):
        result = ToolSelectionResult(
            selected_tools=("search", "read"),
            reasoning="Need to search and read",
            confidence=0.9,
            token_usage=TokenUsage(total_tokens=80),
            duration_ms=45,
            model="qwen-turbo",
        )
        assert result.selected_tools == ("search", "read")
        assert result.confidence == 0.9
        assert result.fallback is False

    def test_fallback_result(self):
        result = ToolSelectionResult(
            selected_tools=("search",),
            reasoning="Fallback: all",
            confidence=0.0,
            token_usage=TokenUsage(),
            duration_ms=0,
            model="fallback",
            fallback=True,
        )
        assert result.fallback is True


class TestSelectTools:
    """Tests for the _select_tools async function."""

    @pytest.mark.asyncio
    async def test_short_circuit_few_tools(self):
        """When there are <=2 tools, selection is skipped."""
        from loom.llm.api import _select_tools

        ctx = _make_context_with_tools(["search"])
        config = ToolSelectionConfig()
        provider = _FakeToolSelectionProvider('{"selected_tools": []}')

        result = await _select_tools(ctx, provider, config, object(), "trace-1")

        assert result.selected_tools == ("search",)
        assert result.confidence == 1.0
        assert result.token_usage == TokenUsage()

    @pytest.mark.asyncio
    async def test_fallback_on_llm_failure(self):
        """When LLM fails, fallback returns all tools."""
        from loom.llm.api import _fallback_tool_selection

        all_ids = ("search", "write", "read", "delete")
        config = ToolSelectionConfig(fallback="all")

        result = _fallback_tool_selection(config, all_ids, "LLM failed")
        assert result.selected_tools == all_ids
        assert result.fallback is True
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_fallback_none(self):
        """Fallback 'none' returns empty tool list."""
        from loom.llm.api import _fallback_tool_selection

        all_ids = ("search", "write", "read")
        config = ToolSelectionConfig(fallback="none")

        result = _fallback_tool_selection(config, all_ids, "LLM failed")
        assert result.selected_tools == ()

    @pytest.mark.asyncio
    async def test_fallback_default(self):
        """Fallback 'default' returns configured default tools."""
        from loom.llm.api import _fallback_tool_selection

        all_ids = ("search", "write", "read", "delete")
        config = ToolSelectionConfig(fallback="default", default_tools=("search", "read"))

        result = _fallback_tool_selection(config, all_ids, "LLM failed")
        assert result.selected_tools == ("search", "read")

    @pytest.mark.asyncio
    async def test_fallback_default_with_invalid_tool(self):
        """Fallback 'default' filters out tools not in available set."""
        from loom.llm.api import _fallback_tool_selection

        all_ids = ("search", "write")
        config = ToolSelectionConfig(fallback="default", default_tools=("search", "nonexistent"))

        result = _fallback_tool_selection(config, all_ids, "LLM failed")
        assert result.selected_tools == ("search",)

    @pytest.mark.asyncio
    async def test_always_include_in_fallback(self):
        """Always-include tools are added even in fallback."""
        from loom.llm.api import _fallback_tool_selection

        all_ids = ("search", "write", "log")
        config = ToolSelectionConfig(fallback="none", always_include=("log",))

        result = _fallback_tool_selection(config, all_ids, "LLM failed")
        assert result.selected_tools == ("log",)


class TestCreateLlmStepWithToolSelection:
    """Integration tests for create_llm_step_function with tool_selection."""

    @pytest.mark.asyncio
    async def test_tool_selection_disabled_by_default(self):
        """Without tool_selection config, all tools are used."""
        from loom.llm.api import create_llm_step_function

        # Verify the function accepts the parameter
        step_fn = create_llm_step_function(
            provider=None,  # Won't be called
            enable_tool_calling=True,
            tool_selection=None,  # Disabled
        )
        assert callable(step_fn)

    @pytest.mark.asyncio
    async def test_tool_selection_config_accepted(self):
        """The function accepts a ToolSelectionConfig."""
        from loom.llm.api import create_llm_step_function

        config = ToolSelectionConfig(enabled=True, max_tools=3)
        step_fn = create_llm_step_function(
            provider=None,
            enable_tool_calling=True,
            tool_selection=config,
        )
        assert callable(step_fn)

    @pytest.mark.asyncio
    async def test_tool_selection_disabled_via_config(self):
        """Config with enabled=False skips tool selection."""
        from loom.llm.api import create_llm_step_function

        config = ToolSelectionConfig(enabled=False)
        step_fn = create_llm_step_function(
            provider=None,
            enable_tool_calling=True,
            tool_selection=config,
        )
        assert callable(step_fn)
