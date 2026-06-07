import asyncio
import json

from loom.examples import run_llm_loop


def test_llm_loop_loads_env_config_when_api_key_is_not_explicit(tmp_path):
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
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "reasoning": "Env config is enough",
                                        "action": {"kind": "none", "description": "Stop"},
                                        "alternatives": [],
                                        "confidence": 0.9,
                                    }
                                )
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
                },
            }

        result = await run_llm_loop({"env_path": env_path, "env": {}, "http_client": http_client})

        assert result.ok
        assert result.value.metrics.steps == 1
        assert result.value.context.state.decisions[-1].reasoning == "Env config is enough"
        assert calls[0][0] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        assert calls[0][1]["headers"]["Authorization"] == "Bearer test-loom-key"
        assert calls[0][1]["body"]["model"] == "qwen3.6-max-preview"

    asyncio.run(scenario())
