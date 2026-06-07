import asyncio

from loom.examples import (
    make_initial_counter_context,
    make_minimal_counter_loop,
)
from loom.runtime import (
    create,
    run,
)


def test_minimal_counter_loop_runs_through_public_api():
    async def scenario():
        context = make_initial_counter_context(3)
        handle = create(make_minimal_counter_loop()).unwrap()
        result = (await run(handle, context)).unwrap()

        assert len(result.context.state.observations) == 3
        assert len(result.context.state.decisions) == 3
        assert len(result.traces) == 3
        assert result.metrics.steps == 3
        assert [observation.value for observation in result.context.state.observations] == [
            {"counter": 1},
            {"counter": 2},
            {"counter": 3},
        ]

    asyncio.run(scenario())
