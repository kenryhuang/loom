import asyncio

from loom.examples import run_trace_query_example


def test_trace_query_example_runs(tmp_path):
    async def scenario():
        result = await run_trace_query_example(tmp_path)
        assert result.ok
        assert result.value["summary"]["count"] >= 1
        assert result.value["manifest"].record_count >= 1

    asyncio.run(scenario())
