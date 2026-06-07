"""
Complex project-level test: Context boundary and knowledge propagation.

Tests the full context isolation and knowledge flow system:
1. Parent → child projection with tool/resource filtering
2. Child execution with independent state
3. Child output emission and merge back to parent
4. Knowledge conflict detection during merge
5. Context patch operations (append observation, add knowledge, replace goal)
6. Knowledge view search across facts/heuristics/memories
7. Multi-level nesting with knowledge propagation chain

Exercises: core models (project, merge_child_output, apply_context_patch,
KnowledgeView), runtime, composition.
"""

from __future__ import annotations

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
    PendingLoop,
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
    new_loop_id,
    new_run_id,
    now_iso,
    project,
)


def _make_rich_parent_context(
    *,
    tools: tuple[ToolRef, ...] = (),
    resources: tuple[ResourceRef, ...] = (),
    facts: tuple[KnowledgeItem, ...] = (),
    heuristics: tuple[KnowledgeItem, ...] = (),
    observations: tuple[Observation, ...] = (),
):
    """Create a parent context with full affordances and knowledge."""
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(role="parent orchestrator"),
            goal=GoalLayer(objective="Parent orchestration goal"),
            state=StateLayer(observations=observations),
            knowledge=empty_knowledge(facts=facts, heuristics=heuristics),
            affordances=empty_affordances(tools=tools, resources=resources),
        )
    )


class TestContextProjection:
    """Test parent → child context projection with filtering."""

    def test_project_filters_tools(self):
        search = ToolRef("search", "Search tool")
        write = ToolRef("write", "Write tool")
        delete = ToolRef("delete", "Delete tool")
        parent = _make_rich_parent_context(tools=(search, write, delete))

        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
            tool_ids=("search", "write"),
        ).unwrap()

        tool_ids = {t.id for t in child.affordances.tools}
        assert tool_ids == {"search", "write"}
        assert "delete" not in tool_ids

    def test_project_filters_resources(self):
        notes = ResourceRef("notes", "file", "notes.md", "read")
        config = ResourceRef("config", "file", "config.yaml", "read")
        secrets = ResourceRef("secrets", "file", "secrets.env", "read")
        parent = _make_rich_parent_context(resources=(notes, config, secrets))

        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
            resource_ids=("notes",),
        ).unwrap()

        resource_ids = {r.id for r in child.affordances.resources}
        assert resource_ids == {"notes"}

    def test_project_preserves_knowledge(self):
        fact = KnowledgeItem("fact-1", "fact", "Shared knowledge", 0.9, now_iso())
        parent = _make_rich_parent_context(facts=(fact,))

        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
        ).unwrap()

        assert len(child.knowledge.facts) == 1
        assert child.knowledge.facts[0].id == "fact-1"

    def test_project_creates_fresh_state(self):
        obs = Observation("parent-obs", "parent", {"data": "value"}, now_iso())
        parent = _make_rich_parent_context(observations=(obs,))

        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
        ).unwrap()

        assert len(child.state.observations) == 0
        assert child.parent_context_id == parent.id

    def test_project_with_state_summary(self):
        obs1 = Observation("obs-1", "parent", {}, now_iso())
        obs2 = Observation("obs-2", "parent", {}, now_iso())
        parent = _make_rich_parent_context(observations=(obs1, obs2))

        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
            include_state_summary=True,
        ).unwrap()

        assert len(child.state.observations) == 1
        summary_obs = child.state.observations[0]
        assert summary_obs.source == "project"
        assert summary_obs.value["observationCount"] == 2


class TestChildOutputMerge:
    """Test child output emission and merge back to parent."""

    def test_merge_child_output_basic(self):
        parent = _make_rich_parent_context()
        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
        ).unwrap()

        child_with_obs = freeze_context(
            replace(
                child,
                state=StateLayer(
                    observations=(Observation("child-obs", "child", {"result": "done"}, now_iso()),),
                ),
            )
        )

        output = emit_child_output(child_with_obs, status="completed")
        merged = merge_child_output(parent, output).unwrap()

        # Parent gets child summary observation + child observations
        child_obs = [o for o in merged.state.observations if o.source == "child"]
        assert len(child_obs) >= 1

    def test_merge_child_output_with_knowledge(self):
        parent = _make_rich_parent_context()
        child_fact = KnowledgeItem("child-fact-1", "fact", "Child discovered this", 0.8, now_iso())
        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
        ).unwrap()

        child_with_knowledge = freeze_context(
            replace(
                child,
                knowledge=replace(child.knowledge, facts=(child_fact,)),
                state=StateLayer(),
            )
        )

        output = emit_child_output(child_with_knowledge, status="completed")
        merged = merge_child_output(parent, output, accept_knowledge_ids=("child-fact-1",)).unwrap()

        assert len(merged.knowledge.facts) == 1
        assert merged.knowledge.facts[0].id == "child-fact-1"

    def test_merge_knowledge_conflict_detected(self):
        existing_fact = KnowledgeItem("shared-fact", "fact", "Parent already has this", 0.9, now_iso())
        parent = _make_rich_parent_context(facts=(existing_fact,))
        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
        ).unwrap()

        # Child tries to return the same knowledge ID
        child_with_knowledge = freeze_context(
            replace(
                child,
                knowledge=replace(
                    child.knowledge,
                    facts=(KnowledgeItem("shared-fact", "fact", "Child version", 0.7, now_iso()),),
                ),
                state=StateLayer(),
            )
        )

        output = emit_child_output(child_with_knowledge, status="completed")
        result = merge_child_output(parent, output, accept_knowledge_ids=("shared-fact",))

        assert not result.ok
        assert result.error.code == "MERGE_CONFLICT"

    def test_merge_without_accepting_knowledge(self):
        parent = _make_rich_parent_context()
        child_fact = KnowledgeItem("child-fact", "fact", "Child knowledge", 0.8, now_iso())
        child = project(
            parent,
            GoalLayer(objective="Child goal"),
            identity=IdentityLayer(role="child"),
        ).unwrap()

        child_with_knowledge = freeze_context(
            replace(
                child,
                knowledge=replace(child.knowledge, facts=(child_fact,)),
                state=StateLayer(),
            )
        )

        output = emit_child_output(child_with_knowledge, status="completed")
        # Don't accept any knowledge IDs
        merged = merge_child_output(parent, output, accept_knowledge_ids=()).unwrap()

        # Knowledge should NOT be merged
        assert len(merged.knowledge.facts) == 0

    def test_emit_child_output_includes_decisions(self):
        action = Action("act-1", "custom", "Test action")
        decision = Decision("dec-1", action, "Test reasoning", (), 0.9, now_iso())
        child = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(role="child"),
                goal=GoalLayer(objective="child goal"),
                state=StateLayer(decisions=(decision,)),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(),
            )
        )

        output = emit_child_output(child, status="completed")
        assert len(output.decisions) == 1
        assert output.decisions[0].id == "dec-1"


class TestContextPatch:
    """Test context patch operations."""

    def test_patch_append_observation(self):
        parent = _make_rich_parent_context()
        new_obs = Observation("patched-obs", "patch", {"patched": True}, now_iso())
        patch = ContextPatch(
            base_context_id=parent.id,
            operations=({"op": "appendObservation", "value": new_obs},),
            reason="Test patch",
        )

        result = apply_context_patch(parent, patch)
        assert result.ok
        assert len(result.value.state.observations) == 1
        assert result.value.state.observations[0].id == "patched-obs"

    def test_patch_append_decision(self):
        parent = _make_rich_parent_context()
        action = Action("act-1", "custom", "Patched action")
        decision = Decision("dec-1", action, "Patched reasoning", (), 0.9, now_iso())
        patch = ContextPatch(
            base_context_id=parent.id,
            operations=({"op": "appendDecision", "value": decision},),
            reason="Test patch",
        )

        result = apply_context_patch(parent, patch)
        assert result.ok
        assert len(result.value.state.decisions) == 1

    def test_patch_add_knowledge(self):
        parent = _make_rich_parent_context()
        new_fact = KnowledgeItem("patched-fact", "fact", "Patched knowledge", 0.9, now_iso())
        patch = ContextPatch(
            base_context_id=parent.id,
            operations=({"op": "addKnowledge", "value": new_fact},),
            reason="Test patch",
        )

        result = apply_context_patch(parent, patch)
        assert result.ok
        assert len(result.value.knowledge.facts) == 1
        assert result.value.knowledge.facts[0].id == "patched-fact"

    def test_patch_replace_goal(self):
        parent = _make_rich_parent_context()
        new_goal = GoalLayer(objective="New objective")
        patch = ContextPatch(
            base_context_id=parent.id,
            operations=({"op": "replaceGoal", "value": new_goal},),
            reason="Test patch",
        )

        result = apply_context_patch(parent, patch)
        assert result.ok
        assert result.value.goal.objective == "New objective"

    def test_patch_set_metadata(self):
        parent = _make_rich_parent_context()
        patch = ContextPatch(
            base_context_id=parent.id,
            operations=(
                {"op": "setMetadata", "key": "priority", "value": "high"},
                {"op": "setMetadata", "key": "source", "value": "test"},
            ),
            reason="Test patch",
        )

        result = apply_context_patch(parent, patch)
        assert result.ok
        assert result.value.metadata["priority"] == "high"
        assert result.value.metadata["source"] == "test"

    def test_patch_wrong_base_context_id(self):
        parent = _make_rich_parent_context()
        patch = ContextPatch(
            base_context_id="wrong-id",
            operations=(),
            reason="Test patch",
        )

        result = apply_context_patch(parent, patch)
        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"

    def test_patch_unsupported_operation(self):
        parent = _make_rich_parent_context()
        patch = ContextPatch(
            base_context_id=parent.id,
            operations=({"op": "deleteEverything", "value": None},),
            reason="Test patch",
        )

        result = apply_context_patch(parent, patch)
        assert not result.ok
        assert "Unsupported patch operation" in result.error.message

    def test_patch_clear_pending(self):
        pending = PendingLoop("pending-1", new_loop_id(), GoalLayer(objective="pending"), now_iso())
        parent = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(role="test"),
                goal=GoalLayer(objective="test"),
                state=StateLayer(pending=(pending,)),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(),
            )
        )

        patch = ContextPatch(
            base_context_id=parent.id,
            operations=({"op": "clearPending", "id": "pending-1"},),
            reason="Clear pending",
        )

        result = apply_context_patch(parent, patch)
        assert result.ok
        assert len(result.value.state.pending) == 0


class TestKnowledgeView:
    """Test knowledge view search capabilities."""

    def test_search_by_kind(self):
        fact = KnowledgeItem("fact-1", "fact", "A fact", 0.9, now_iso())
        heuristic = KnowledgeItem("heur-1", "heuristic", "A heuristic", 0.8, now_iso())
        memory = KnowledgeItem("mem-1", "memory", "A memory", 0.7, now_iso())
        knowledge = empty_knowledge(facts=(fact,), heuristics=(heuristic,), memories=(memory,))
        view = create_knowledge_view(knowledge)

        facts = view.search(kind="fact")
        assert len(facts) == 1
        assert facts[0].id == "fact-1"

        heuristics = view.search(kind="heuristic")
        assert len(heuristics) == 1

    def test_search_by_text(self):
        knowledge = empty_knowledge(
            facts=(
                KnowledgeItem("f1", "fact", "Python is great", 0.9, now_iso()),
                KnowledgeItem("f2", "fact", "JavaScript runs everywhere", 0.8, now_iso()),
                KnowledgeItem("f3", "fact", "Rust is memory safe", 0.95, now_iso()),
            ),
        )
        view = create_knowledge_view(knowledge)

        results = view.search(text="python")
        assert len(results) == 1
        assert "Python" in str(results[0].content)

    def test_search_by_min_confidence(self):
        knowledge = empty_knowledge(
            facts=(
                KnowledgeItem("f1", "fact", "High confidence", 0.95, now_iso()),
                KnowledgeItem("f2", "fact", "Medium confidence", 0.7, now_iso()),
                KnowledgeItem("f3", "fact", "Low confidence", 0.3, now_iso()),
            ),
        )
        view = create_knowledge_view(knowledge)

        results = view.search(min_confidence=0.8)
        assert len(results) == 1
        assert results[0].id == "f1"

    def test_search_with_limit(self):
        knowledge = empty_knowledge(
            facts=tuple(KnowledgeItem(f"f{i}", "fact", f"Fact {i}", 0.9, now_iso()) for i in range(10)),
        )
        view = create_knowledge_view(knowledge)

        results = view.search(limit=3)
        assert len(results) == 3

    def test_search_combined_filters(self):
        knowledge = empty_knowledge(
            facts=(
                KnowledgeItem("f1", "fact", "Python web framework", 0.9, now_iso()),
                KnowledgeItem("f2", "fact", "Python data science", 0.85, now_iso()),
                KnowledgeItem("f3", "fact", "JavaScript framework", 0.8, now_iso()),
            ),
            heuristics=(KnowledgeItem("h1", "heuristic", "Python best practice", 0.95, now_iso()),),
        )
        view = create_knowledge_view(knowledge)

        results = view.search(kind="fact", text="python", min_confidence=0.85)
        assert len(results) == 2  # f1 (0.9) and f2 (0.85) both match
        assert results[0].id == "f1"
        assert results[1].id == "f2"


class TestMultiLevelNestingKnowledgePropagation:
    """Test knowledge propagation through multiple nesting levels."""

    def test_three_level_knowledge_flow(self):
        """Grandparent → parent → child, knowledge flows down and merges up."""
        grandparent_fact = KnowledgeItem("gp-fact", "fact", "Grandparent knowledge", 0.95, now_iso())
        grandparent = _make_rich_parent_context(facts=(grandparent_fact,))

        # Project to parent
        parent = project(
            grandparent,
            GoalLayer(objective="Parent task"),
            identity=IdentityLayer(role="parent"),
        ).unwrap()

        # Parent inherits grandparent's knowledge
        assert len(parent.knowledge.facts) == 1
        assert parent.knowledge.facts[0].id == "gp-fact"

        # Parent adds its own knowledge
        parent_fact = KnowledgeItem("p-fact", "fact", "Parent discovered", 0.85, now_iso())
        parent_with_knowledge = freeze_context(
            replace(
                parent,
                knowledge=replace(parent.knowledge, facts=(*parent.knowledge.facts, parent_fact)),
            )
        )

        # Project to child
        child = project(
            parent_with_knowledge,
            GoalLayer(objective="Child task"),
            identity=IdentityLayer(role="child"),
        ).unwrap()

        # Child inherits both grandparent and parent knowledge
        assert len(child.knowledge.facts) == 2
        child_fact_ids = {f.id for f in child.knowledge.facts}
        assert child_fact_ids == {"gp-fact", "p-fact"}

        # Child adds knowledge and merges back to parent
        child_fact = KnowledgeItem("c-fact", "fact", "Child discovered", 0.8, now_iso())
        child_with_knowledge = freeze_context(
            replace(
                child,
                knowledge=replace(child.knowledge, facts=(*child.knowledge.facts, child_fact)),
                state=StateLayer(),
            )
        )

        child_output = emit_child_output(child_with_knowledge, status="completed")
        merged_parent = merge_child_output(
            parent_with_knowledge,
            child_output,
            accept_knowledge_ids=("c-fact",),
        ).unwrap()

        # Parent now has grandparent + parent + child knowledge
        assert len(merged_parent.knowledge.facts) == 3

        # Parent merges back to grandparent
        parent_output = emit_child_output(merged_parent, status="completed")
        merged_grandparent = merge_child_output(
            grandparent,
            parent_output,
            accept_knowledge_ids=("p-fact", "c-fact"),
        ).unwrap()

        # Grandparent now has all knowledge
        assert len(merged_grandparent.knowledge.facts) == 3
        gp_fact_ids = {f.id for f in merged_grandparent.knowledge.facts}
        assert gp_fact_ids == {"gp-fact", "p-fact", "c-fact"}
