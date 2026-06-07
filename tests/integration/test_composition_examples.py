import asyncio

from loom.examples import (
    make_chain_pipeline_example,
    make_fork_reviewers_example,
    make_nested_tool_example,
)


def test_composition_examples_run():
    async def scenario():
        chain_result = await make_chain_pipeline_example()
        nested_result = await make_nested_tool_example()
        fork_result = await make_fork_reviewers_example()

        assert chain_result.ok
        assert nested_result.ok
        assert fork_result.ok

    asyncio.run(scenario())
