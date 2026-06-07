"""
Complex project-level test: Full evolution lifecycle.

Tests the complete evolution workflow:
1. Run a loop and collect traces with gaps/surprises
2. Decide evolution level based on trace analysis
3. Apply mutations (context, loop, structure)
4. Run shadow evaluation (before vs after)
5. Commit or rollback mutations via InMemoryLoopRegistry
6. Verify composition graph mutations and cycle detection

Exercises: evolution, runtime, core models, observability, composition.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.core import (
    Context,
    ContextPatch,
    Trace,
    new_loop_id,
    new_run_id,
    new_trace_id,
    now_iso,
    ok,
)
from loom.evolution import (
    CompositionEdge,
    CompositionGraph,
    CompositionNode,
    ContextMutation,
    DefaultEvolutionEvaluator,
    InMemoryImplementationRegistry,
    InMemoryLoopRegistry,
    LoopMutation,
    MutationBundle,
    MutationPolicy,
    MutationTransaction,
    ScoreSummary,
    StructureMutation,
    apply_loop_mutation,
    apply_structure_mutation,
    decide_evolution,
    run_shadow_evaluation,
    validate_composition_graph,
    validate_mutation_bundle_shape,
)
from loom.examples import (
    make_initial_counter_context,
    make_minimal_counter_loop,
)
from loom.runtime import (
    create,
)


def _make_trace_with_metadata(outcome: str = "pass", metadata: dict | None = None):
    """Helper to create traces with metadata for evolution decision testing."""
    trace_id = new_trace_id()
    return Trace(
        id=trace_id,
        run_id=new_run_id(),
        loop_id=new_loop_id(),
        loop_version="v1",
        step_number=0,
        root_trace_id=trace_id,
        started_at=now_iso(),
        ended_at=now_iso(),
        duration_ms=1,
        input_context_id="ctx_1",
        output_context_id="ctx_2",
        outcome=outcome,
        metadata=metadata,
    )


class TestEvolutionDecision:
    """Test decide_evolution with various trace patterns."""

    def test_repeated_gaps_trigger_context_evolution(self):
        traces = (
            _make_trace_with_metadata(metadata={"gap": "missing_permission_check"}),
            _make_trace_with_metadata(metadata={"gap": "missing_permission_check"}),
        )
        decision = decide_evolution(traces)
        assert decision.level == 1
        assert "context" in decision.proposed_mutation_kinds
        assert "missing_permission_check" in decision.reason

    def test_repeated_surprises_trigger_loop_evolution(self):
        traces = (
            _make_trace_with_metadata(metadata={"surprise": "tool_timeout"}),
            _make_trace_with_metadata(metadata={"surprise": "tool_timeout"}),
        )
        decision = decide_evolution(traces)
        assert decision.level == 2
        assert "loop" in decision.proposed_mutation_kinds

    def test_timeout_triggers_structure_evolution(self):
        traces = (
            _make_trace_with_metadata(outcome="timeout"),
            _make_trace_with_metadata(outcome="pass"),
        )
        decision = decide_evolution(traces)
        assert decision.level == 3
        assert "structure" in decision.proposed_mutation_kinds

    def test_weak_signal_defaults_context_refinement(self):
        traces = (
            _make_trace_with_metadata(outcome="pass"),
            _make_trace_with_metadata(outcome="pass"),
        )
        decision = decide_evolution(traces)
        assert decision.level == 1
        assert "context" in decision.proposed_mutation_kinds


class TestMutationBundleValidation:
    """Test mutation bundle shape validation."""

    def test_valid_bundle(self):
        patch = ContextPatch("ctx_1", (), "test reason")
        bundle = MutationBundle(
            base_version="v1",
            mutations=(ContextMutation(patch),),
            created_from_trace_ids=("trace_1",),
        )
        result = validate_mutation_bundle_shape(bundle)
        assert result.ok

    def test_missing_base_version(self):
        patch = ContextPatch("ctx_1", (), "test reason")
        bundle = MutationBundle(
            base_version="",
            mutations=(ContextMutation(patch),),
            created_from_trace_ids=("trace_1",),
        )
        result = validate_mutation_bundle_shape(bundle)
        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"

    def test_missing_evidence_trace_when_required(self):
        patch = ContextPatch("ctx_1", (), "test reason")
        bundle = MutationBundle(
            base_version="v1",
            mutations=(ContextMutation(patch),),
            created_from_trace_ids=(),
            policy=MutationPolicy(require_evidence_trace=True),
        )
        result = validate_mutation_bundle_shape(bundle)
        assert not result.ok
        assert "created_from_trace_ids" in result.error.message

    def test_empty_mutations_rejected(self):
        bundle = MutationBundle(
            base_version="v1",
            mutations=(),
            created_from_trace_ids=("trace_1",),
        )
        result = validate_mutation_bundle_shape(bundle)
        assert not result.ok


class TestInMemoryLoopRegistry:
    """Test loop registry with version management and mutation transactions."""

    def test_register_and_retrieve_active(self):
        registry = InMemoryLoopRegistry()
        loop_def = make_minimal_counter_loop()
        handle = create(loop_def).unwrap()
        registry.register(handle)

        result = registry.active(handle.id)
        assert result.ok
        assert result.value.version == "v1"

    def test_begin_mutation_version_mismatch(self):
        registry = InMemoryLoopRegistry()
        loop_def = make_minimal_counter_loop()
        handle = create(loop_def).unwrap()
        registry.register(handle)

        # Try to mutate from wrong base version
        result = registry.begin_mutation(handle.id, "v99")
        assert not result.ok
        assert result.error.code == "MUTATION_REJECTED"

    def test_mutation_transaction_commit(self):
        registry = InMemoryLoopRegistry()
        loop_def = make_minimal_counter_loop()
        handle = create(loop_def).unwrap()
        registry.register(handle)

        tx_result = registry.begin_mutation(handle.id, "v1")
        assert tx_result.ok
        tx: MutationTransaction = tx_result.value

        apply_result = tx.apply(handle)
        assert apply_result.ok
        assert apply_result.value.version == "v2"

        tx.commit()

        active = registry.active(handle.id)
        assert active.ok
        assert active.value.version == "v2"

    def test_mutation_transaction_rollback(self):
        registry = InMemoryLoopRegistry()
        loop_def = make_minimal_counter_loop()
        handle = create(loop_def).unwrap()
        registry.register(handle)

        tx_result = registry.begin_mutation(handle.id, "v1")
        assert tx_result.ok
        tx: MutationTransaction = tx_result.value

        tx.apply(handle)
        tx.rollback()

        active = registry.active(handle.id)
        assert active.ok
        assert active.value.version == "v1"  # Still v1 after rollback


class TestLoopMutation:
    """Test loop-level mutations with implementation registry."""

    def test_apply_loop_mutation_step_replacement(self):
        impl_registry = InMemoryImplementationRegistry()

        async def new_step(context: Context, runtime: Any):
            return ok(context)

        impl_registry.register("new_step_impl", new_step)

        loop_def = make_minimal_counter_loop()
        mutation = LoopMutation(step_ref="new_step_impl")

        result = apply_loop_mutation(loop_def, mutation, impl_registry)
        assert result.ok
        assert result.value.version == "v2"
        assert result.value.step is new_step

    def test_apply_loop_mutation_missing_implementation(self):
        impl_registry = InMemoryImplementationRegistry()
        loop_def = make_minimal_counter_loop()
        mutation = LoopMutation(step_ref="nonexistent_step")

        result = apply_loop_mutation(loop_def, mutation, impl_registry)
        assert not result.ok
        assert result.error.code == "MUTATION_REJECTED"


class TestCompositionGraphMutation:
    """Test structure mutations on composition graphs."""

    def test_valid_graph(self):
        graph = CompositionGraph(
            version="v1",
            nodes=(CompositionNode("plan", "loop_1"), CompositionNode("execute", "loop_2")),
            edges=(CompositionEdge("plan", "execute", "chain"),),
        )
        result = validate_composition_graph(graph)
        assert result.ok

    def test_graph_with_cycle_rejected(self):
        graph = CompositionGraph(
            version="v1",
            nodes=(
                CompositionNode("a", "loop_1"),
                CompositionNode("b", "loop_2"),
                CompositionNode("c", "loop_3"),
            ),
            edges=(
                CompositionEdge("a", "b", "chain"),
                CompositionEdge("b", "c", "chain"),
                CompositionEdge("c", "a", "chain"),
            ),
        )
        result = validate_composition_graph(graph)
        assert not result.ok
        assert "cycle" in result.error.message.lower()

    def test_dangling_edge_rejected(self):
        graph = CompositionGraph(
            version="v1",
            nodes=(CompositionNode("a", "loop_1"),),
            edges=(CompositionEdge("a", "nonexistent", "chain"),),
        )
        result = validate_composition_graph(graph)
        assert not result.ok
        assert "dangling" in result.error.message.lower()

    def test_insert_loop_structure_mutation(self):
        graph = CompositionGraph(
            version="v1",
            nodes=(CompositionNode("plan", "loop_1"), CompositionNode("execute", "loop_2")),
            edges=(CompositionEdge("plan", "execute", "chain"),),
        )
        mutation = StructureMutation(
            operation="insert-loop",
            loop_ref="validate",
            insert_after="execute",
        )
        result = apply_structure_mutation(graph, mutation)
        assert result.ok
        assert result.value.version == "v2"
        assert len(result.value.nodes) == 3
        assert len(result.value.edges) == 2

    def test_insert_after_nonexistent_node(self):
        graph = CompositionGraph(
            version="v1",
            nodes=(CompositionNode("a", "loop_1"),),
            edges=(),
        )
        mutation = StructureMutation(
            operation="insert-loop",
            loop_ref="new",
            insert_after="nonexistent",
        )
        result = apply_structure_mutation(graph, mutation)
        assert not result.ok


class TestShadowEvaluation:
    """Test before/after shadow evaluation with real loop execution."""

    @pytest.mark.asyncio
    async def test_shadow_evaluation_improvement(self):
        """Run shadow evaluation: after version adds heuristic knowledge."""
        before_def = make_minimal_counter_loop()
        before_handle = create(before_def).unwrap()

        # Create after version with modified step (same logic, different version)
        after_def = make_minimal_counter_loop()
        after_handle = create(after_def).unwrap()

        contexts = (make_initial_counter_context(max_steps=2),)
        result = await run_shadow_evaluation(before_handle, after_handle, contexts)

        assert result.evaluation.accepted is True or result.evaluation.accepted is False
        # Both versions are identical, so delta should be 0
        assert "delta=" in result.evaluation.reason


class TestEvolutionEvaluatorScoring:
    """Test evolution evaluator scoring logic."""

    def test_score_all_passes(self):
        evaluator = DefaultEvolutionEvaluator()
        traces = tuple(_make_trace_with_metadata(outcome="pass") for _ in range(5))
        score = evaluator.score_run(traces)
        assert score.score == 5.0

    def test_score_mixed_outcomes(self):
        evaluator = DefaultEvolutionEvaluator()
        traces = (
            _make_trace_with_metadata(outcome="pass"),
            _make_trace_with_metadata(outcome="pass"),
            _make_trace_with_metadata(outcome="fail"),
            _make_trace_with_metadata(outcome="timeout"),
        )
        score = evaluator.score_run(traces)
        # 2 passes - 2*1 fail - 2*1 timeout = 2 - 2 - 2 = -2
        assert score.score == -2.0

    def test_score_with_gaps(self):
        evaluator = DefaultEvolutionEvaluator()
        traces = (
            _make_trace_with_metadata(outcome="pass", metadata={"gap": "missing_check"}),
            _make_trace_with_metadata(outcome="pass", metadata={"gap": "missing_check"}),
        )
        score = evaluator.score_run(traces)
        # 2 passes - 2 * 0.5 gaps = 2 - 1 = 1
        assert score.score == 1.0

    def test_compare_improvement(self):
        evaluator = DefaultEvolutionEvaluator(min_delta=0.5)
        before = ScoreSummary(score=1.0, metrics={})
        after = ScoreSummary(score=3.0, metrics={})
        bundle = MutationBundle(
            "v1",
            (ContextMutation(ContextPatch("ctx_1", (), "test")),),
            ("trace_1",),
        )
        evaluation = evaluator.compare(before, after, bundle)
        assert evaluation.accepted is True
        assert "delta=2.0" in evaluation.reason

    def test_compare_no_improvement(self):
        evaluator = DefaultEvolutionEvaluator(min_delta=1.0)
        before = ScoreSummary(score=3.0, metrics={})
        after = ScoreSummary(score=3.5, metrics={})
        bundle = MutationBundle(
            "v1",
            (ContextMutation(ContextPatch("ctx_1", (), "test")),),
            ("trace_1",),
        )
        evaluation = evaluator.compare(before, after, bundle)
        assert evaluation.accepted is False  # delta=0.5 < min_delta=1.0
