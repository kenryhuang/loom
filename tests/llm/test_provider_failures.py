import asyncio
import urllib.error

import pytest

from loom.llm.api import LlmMessage, OpenAIProvider, request_llm_response


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure,code", [(TimeoutError(), "TIMEOUT"), (urllib.error.URLError(TimeoutError()), "TIMEOUT"),
                                         (RuntimeError(), "LLM_FAILED")])
async def test_provider_exceptions_keep_typed_nonempty_errors(stream, failure, code):
    async def transport(*args):
        raise failure

    provider = OpenAIProvider(api_key="fixture", model="fixture", http_client=transport)
    result = await request_llm_response(provider, [LlmMessage("user", "Test")], stream=stream)
    assert not result.ok
    assert result.error.code == code
    assert result.error.message
    assert result.error.cause["name"] == type(failure).__name__


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_deadlines_and_cancellation_are_not_swallowed_by_provider(stream):
    async def transport(*args):
        await asyncio.sleep(10)

    provider = OpenAIProvider(api_key="fixture", model="fixture", http_client=transport)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.01):
            await request_llm_response(provider, [LlmMessage("user", "Test")], stream=stream)
    task = asyncio.create_task(request_llm_response(provider, [LlmMessage("user", "Test")], stream=stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
