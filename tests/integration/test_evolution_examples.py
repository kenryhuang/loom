import asyncio

from loom.examples import (
    make_level1_evolution_example,
    make_structure_evolution_example,
)


def test_evolution_examples_run():
    async def scenario():
        level1 = await make_level1_evolution_example()
        structure = await make_structure_evolution_example()

        assert level1.ok
        assert any(item.id == "heuristic.permission-ownership-first" for item in level1.value.knowledge.heuristics)
        assert structure.ok
        assert any(node.id == "validate" for node in structure.value.nodes)

    asyncio.run(scenario())
