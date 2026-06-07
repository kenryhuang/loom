from loom.examples import run_context_boundary_example


def test_context_boundary_example_projects_emits_and_merges():
    result = run_context_boundary_example()

    assert result.ok
    assert result.value.parent.role == "parent"
    assert result.value.child.parent_context_id == result.value.parent_context.id
    assert len(result.value.merged_context.state.observations) >= 2
    assert any(item.id == "child-fact" for item in result.value.merged_context.knowledge.facts)
