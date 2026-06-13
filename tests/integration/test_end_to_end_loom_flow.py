import asyncio
import json

from loom.core import (
    Observation,
    now_iso,
    ok,
)
from loom.examples import (
    make_initial_llm_context,
    make_llm_loop_definition,
)
from loom.llm import create_env_openai_provider
from loom.observability import InMemoryTraceStore
from loom.runtime import (
    create,
    create_runtime_registry,
    run,
)


def test_end_to_end_llm_loop_runs_tool_and_records_queryable_trace(tmp_path):
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
    http_requests = []
    tool_invocations = []

    async def http_client(url, request):
        http_requests.append((url, request))
        if len(http_requests) == 1:
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
                                        "id": "call_search_notes",
                                        "type": "function",
                                        "function": {
                                            "name": "search-notes",
                                            "arguments": json.dumps({"query": "loom runtime traces"}),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112},
                },
            }
        return {
            "status": 200,
            "ok": True,
            "json": {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "reasoning": "The tool result identifies trace-backed loop execution as the next useful path.",
                                    "action": {
                                        "kind": "tool",
                                        "target": "search-notes",
                                        "description": "Use the retrieved trace guidance",
                                        "input": {"query": "loom runtime traces"},
                                    },
                                    "alternatives": [
                                        {
                                            "kind": "none",
                                            "description": "Stop without using project notes",
                                        }
                                    ],
                                    "confidence": 0.92,
                                }
                            )
                        },
                    }
                ],
                "usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
            },
        }

    async def search_notes(input_value, options):
        tool_invocations.append((input_value, options))
        return ok(
            Observation(
                "search-notes-e2e-observation",
                "search-notes",
                {
                    "input": input_value,
                    "matches": [
                        {
                            "title": "Loom runtime",
                            "summary": "A loop step records decisions, actions, observations, and traces.",
                        }
                    ],
                },
                now_iso(),
            )
        )

    async def scenario():
        provider = create_env_openai_provider(env_path=env_path, env={}, http_client=http_client).unwrap()
        trace_store = InMemoryTraceStore()
        loop = create(
            make_llm_loop_definition({}, provider=provider),
            trace_store=trace_store,
            registry=create_runtime_registry(tools={"search-notes": search_notes}),
        ).unwrap()

        result = (await run(loop, make_initial_llm_context(), max_steps=1)).unwrap()

        assert result.metrics.steps == 1
        assert result.metrics.trace_count == 1
        assert len(result.context.state.decisions) == 1
        assert [observation.source for observation in result.context.state.observations] == ["search-notes", "llm"]
        assert result.context.state.decisions[0].action.target == "search-notes"
        assert result.context.state.decisions[0].confidence == 0.92

        assert len(http_requests) == 2
        first_url, first_request = http_requests[0]
        assert first_url == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        assert first_request["headers"]["Authorization"] == "Bearer test-loom-key"
        assert first_request["body"]["model"] == "qwen3.6-max-preview"
        assert first_request["body"]["tools"][0]["function"]["name"] == "search-notes"
        assert [message["role"] for message in first_request["body"]["messages"]] == ["system", "user"]
        assert [message["role"] for message in http_requests[1][1]["body"]["messages"]] == ["system", "user", "assistant", "tool"]

        assert tool_invocations[0][0] == {"query": "loom runtime traces"}
        assert tool_invocations[0][1]["metadata"]["tool_call_id"] == "call_search_notes"

        traces = [trace async for trace in loop.trace_reader.query({"run_id": result.context.run_id, "tags": ("llm",)})]
        assert len(traces) == 1
        trace = traces[0]
        assert trace.outcome == "pass"
        assert trace.metadata["model"] == "qwen3.6-max-preview"
        assert trace.metadata["tokenUsage"]["totalTokens"] == 232
        assert [observation.source for observation in trace.observations] == ["search-notes", "llm"]
        assert trace.decisions[0] == result.context.state.decisions[0]

        summary = await loop.trace_reader.summarize({"run_id": result.context.run_id})
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
