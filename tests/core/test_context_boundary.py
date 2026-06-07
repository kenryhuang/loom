from dataclasses import replace

from loom.core import (
    Action,
    Context,
    ContextPatch,
    Decision,
    GoalLayer,
    IdentityLayer,
    KnowledgeItem,
    Observation,
    ResourceRef,
    StateLayer,
    ToolRef,
    apply_context_patch,
    create_knowledge_view,
    emit_child_output,
    empty_affordances,
    empty_knowledge,
    freeze_context,
    merge_child_output,
    new_context_id,
    new_run_id,
    project,
)

NOW = "2026-06-04T00:00:00.000Z"


def test_context_patch_and_knowledge_view():
    context = make_parent_context()
    observation = Observation("obs-2", "test", {"count": 1}, NOW)
    action = Action("action-1", "custom", "Record")
    decision = Decision("decision-1", action, "Need it", (), 0.8, NOW)
    heuristic = KnowledgeItem("heuristic-1", "heuristic", "Prefer reversible steps.", 0.8, NOW)
    patch = ContextPatch(
        base_context_id=context.id,
        operations=(
            {"op": "appendObservation", "value": observation},
            {"op": "appendDecision", "value": decision},
            {"op": "addKnowledge", "value": heuristic},
            {"op": "setMetadata", "key": "phase", "value": "one"},
        ),
        reason="exercise patch",
    )

    result = apply_context_patch(context, patch)
    assert result.ok
    next_context = result.value
    assert len(next_context.state.observations) == 2
    assert len(context.state.observations) == 1
    assert next_context.knowledge.heuristics[0].id == "heuristic-1"
    assert next_context.metadata["phase"] == "one"

    view = create_knowledge_view(next_context.knowledge)
    assert [item.id for item in view.search(text="reversible")] == ["heuristic-1"]
    assert [item.id for item in view.search(kind="fact", min_confidence=0.9)] == ["fact-1"]


def test_project_emit_and_merge_child_output():
    parent = make_parent_context()
    child = project(
        parent,
        GoalLayer(objective="Child goal"),
        identity=IdentityLayer(role="child"),
        tool_ids=("search",),
        resource_ids=("notes",),
    ).unwrap()

    assert child.parent_context_id == parent.id
    assert child.identity.role == "child"
    assert child.goal.objective == "Child goal"
    assert child.state.observations == ()
    assert [tool.id for tool in child.affordances.tools] == ["search"]
    assert [resource.id for resource in child.affordances.resources] == ["notes"]
    assert child.knowledge.facts[0].id == "fact-1"

    child_observation = Observation("child-obs", "child", {"found": True}, NOW)
    child_fact = KnowledgeItem("child-fact", "fact", "Child learned this.", 0.7, NOW)
    child_context = freeze_context(
        replace(
            child,
            state=StateLayer(observations=(child_observation,)),
            knowledge=replace(child.knowledge, facts=(*child.knowledge.facts, child_fact)),
        )
    )

    output = emit_child_output(child_context, status="completed")
    assert output.status == "completed"
    assert [item.id for item in output.knowledge_candidates] == ["fact-1", "child-fact"]

    merged = merge_child_output(parent, output, accept_knowledge_ids=("child-fact",)).unwrap()
    assert len(merged.state.observations) == 3
    assert merged.state.observations[-1].id == "child-obs"
    assert any(item.id == "child-fact" for item in merged.knowledge.facts)

    conflict = merge_child_output(parent, output, accept_knowledge_ids=("fact-1",))
    assert not conflict.ok
    assert conflict.error.code == "MERGE_CONFLICT"


def make_parent_context():
    fact = KnowledgeItem("fact-1", "fact", "The index contains project notes.", 0.9, NOW)
    observation = Observation("obs-1", "parent", {"ready": True}, NOW)
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=NOW,
            identity=IdentityLayer(role="parent"),
            goal=GoalLayer(objective="Parent goal"),
            state=StateLayer(observations=(observation,)),
            knowledge=empty_knowledge(facts=(fact,)),
            affordances=empty_affordances(
                tools=(
                    ToolRef("search", "Search notes"),
                    ToolRef("write", "Write notes"),
                ),
                resources=(
                    ResourceRef("notes", "file", "notes.md", "read"),
                    ResourceRef("secrets", "file", "secret", "read"),
                ),
            ),
        )
    )
