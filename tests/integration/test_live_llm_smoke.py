import asyncio
import os
from pathlib import Path

import pytest

from loom.core import (
    Context,
    GoalLayer,
    IdentityLayer,
    MinimalLoopDefinition,
    Observation,
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
    LlmMessage,
    create_env_openai_provider,
    create_llm_step_function,
    to_llm_tool,
)
from loom.observability import InMemoryTraceStore
from loom.runtime import (
    create,
    create_runtime_registry,
    run,
)


def _live_env_path() -> Path:
    return Path(os.environ.get("LOOM_LIVE_ENV_FILE", ".env"))


def _skip_unless_live_llm_enabled(env_path: Path) -> None:
    if os.environ.get("LOOM_RUN_LIVE_LLM") != "1":
        pytest.skip("set LOOM_RUN_LIVE_LLM=1 to run live LLM tests against the configured provider")
    if not env_path.exists():
        pytest.skip(f"LLM env file does not exist: {env_path}")


def test_live_env_provider_api_basic_chat_completion():
    env_path = _live_env_path()
    _skip_unless_live_llm_enabled(env_path)

    async def scenario():
        provider_result = create_env_openai_provider(
            env_path=env_path,
            max_tokens=int(os.environ.get("LOOM_LIVE_MAX_TOKENS", "128")),
            temperature=float(os.environ.get("LOOM_LIVE_TEMPERATURE", "0")),
        )
        assert provider_result.ok, provider_result.error.message
        provider = provider_result.value

        response = await provider.chat((LlmMessage("user", "Return exactly this text and nothing else: loom-live-provider-ok"),))

        assert response.ok, response.error.message
        assert response.value.content or response.value.tool_calls

    asyncio.run(scenario())


def test_live_env_provider_api_required_tool_choice_contract():
    env_path = _live_env_path()
    _skip_unless_live_llm_enabled(env_path)

    async def scenario():
        provider_result = create_env_openai_provider(
            env_path=env_path,
            max_tokens=int(os.environ.get("LOOM_LIVE_MAX_TOKENS", "128")),
            temperature=float(os.environ.get("LOOM_LIVE_TEMPERATURE", "0")),
        )
        assert provider_result.ok, provider_result.error.message
        provider = provider_result.value
        tool = to_llm_tool(
            ToolRef(
                "diagnose-provider",
                "Return provider diagnostic data.",
                input_schema={
                    "type": "object",
                    "properties": {"probe": {"type": "string"}},
                    "required": ["probe"],
                    "additionalProperties": False,
                },
            )
        )

        response = await provider.chat(
            (LlmMessage("user", 'Call diagnose-provider with {"probe":"env"}; do not answer in text.'),),
            tools=(tool,),
            tool_choice="required",
        )
        if not response.ok:
            message = response.error.message if response.error else "unknown provider error"
            if "tool_choice" in message or "tool_choice" in str(getattr(response.error, "cause", "")):
                pytest.xfail(f"configured provider rejects tool_choice=required: {message}")
            assert response.ok, message
        assert response.value.tool_calls, f"provider returned no native tool calls; finish_reason={response.value.finish_reason!r}"

    asyncio.run(scenario())


def test_live_llm_loop_runs_full_runtime_tool_trace_chain():
    env_path = _live_env_path()
    _skip_unless_live_llm_enabled(env_path)

    async def scenario():
        provider_result = create_env_openai_provider(
            env_path=env_path,
            max_tokens=int(os.environ.get("LOOM_LIVE_MAX_TOKENS", "512")),
            temperature=float(os.environ.get("LOOM_LIVE_TEMPERATURE", "0")),
        )
        assert provider_result.ok, provider_result.error.message
        provider = provider_result.value
        tool_invocations = []

        async def search_notes(input_value, options):
            tool_invocations.append((input_value, options))
            return ok(
                Observation(
                    "live-search-notes-observation",
                    "search-notes",
                    {
                        "input": input_value,
                        "matches": [
                            {
                                "title": "Live Loom smoke test",
                                "summary": "The full chain should use env config, real LLM, runtime tools, and trace recording.",
                            }
                        ],
                    },
                    now_iso(),
                )
            )

        context = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(
                    role="live Loom smoke-test planner",
                    constraints=(
                        {
                            "id": "use-search-notes",
                            "description": "Call the search-notes tool exactly once before returning the final JSON decision.",
                            "severity": "must",
                        },
                        {
                            "id": "json-only",
                            "description": "After the tool result, return only valid JSON in the required Loom decision format.",
                            "severity": "must",
                        },
                    ),
                ),
                goal=GoalLayer(
                    objective=(
                        "Run a live end-to-end Loom smoke test. Use search-notes with query "
                        "'live loom full chain smoke', then choose a final action that targets search-notes."
                    ),
                    budget={"max_steps": 1, "max_tokens": 8000},
                ),
                state=empty_state(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(
                    tools=(
                        ToolRef(
                            "search-notes",
                            "Search local Loom smoke-test notes. Use this tool before making the final decision.",
                            input_schema={
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                                "additionalProperties": False,
                            },
                        ),
                    )
                ),
            )
        )
        loop_definition = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="live LLM full-chain smoke loop"),
            goal=GoalLayer(objective="Run one live LLM-backed Loom step"),
            step=create_llm_step_function(provider, enable_tool_calling=True, max_tool_calls_per_step=2),
            done=lambda next_context, _runtime: ok(len(next_context.state.decisions) > 0),
        )
        trace_store = InMemoryTraceStore()
        loop = create(
            loop_definition,
            trace_store=trace_store,
            registry=create_runtime_registry(tools={"search-notes": search_notes}),
        ).unwrap()

        result = await run(loop, context, max_steps=1)

        assert result.ok, result.error.message
        assert result.value.metrics.steps == 1
        assert result.value.metrics.trace_count == 1
        assert len(tool_invocations) == 1
        assert tool_invocations[0][0]["query"]
        assert tool_invocations[0][1]["metadata"]["tool_name"] == "search-notes"

        sources = [observation.source for observation in result.value.context.state.observations]
        assert "search-notes" in sources
        assert "llm" in sources
        assert len(result.value.context.state.decisions) == 1
        assert result.value.context.state.decisions[0].metadata["parseFallback"] is False

        traces = [trace async for trace in loop.trace_reader.query({"run_id": context.run_id, "tags": ("llm",)})]
        assert len(traces) == 1
        trace = traces[0]
        assert trace.outcome == "pass"
        assert trace.metadata["model"] == provider.model
        assert trace.metadata["tokenUsage"]["totalTokens"] > 0
        assert [observation.source for observation in trace.observations] == ["search-notes", "llm"]

        summary = await loop.trace_reader.summarize({"run_id": context.run_id})
        assert summary["count"] == 1
        assert summary["by_outcome"] == {"pass": 1}

        event_types = [event["type"] for event in trace_store.events()]
        assert event_types == [
            "run.started",
            "step.started",
            "llm.requested",
            "llm.completed",
            "tool.started",
            "tool.completed",
            "llm.requested",
            "llm.completed",
            "decision.recorded",
            "action.started",
            "observation.recorded",
            "observation.recorded",
            "action.completed",
            "action.recorded",
            "step.completed",
            "run.completed",
        ]

    asyncio.run(scenario())
