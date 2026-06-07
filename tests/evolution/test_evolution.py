import asyncio

from loom.core import (
    ContextPatch,
    Trace,
    as_step_number,
    make_loom_error,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
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
from loom.runtime import create

NOW = "2026-06-04T00:00:00.000Z"


def test_mutation_shape_strategy_registry_and_evaluator():
    handle = create(make_minimal_counter_loop()).unwrap()
    registry = InMemoryLoopRegistry()
    registry.register(handle)
    assert registry.active(handle.id).unwrap().version == "v1"

    bundle = MutationBundle(
        base_version="v1",
        mutations=(
            ContextMutation(
                patch=ContextPatch(handle.id, (), "record heuristic"),
                expected_impact={"kind": "quality"},
            ),
        ),
        created_from_trace_ids=("trace-gap-1",),
        policy=MutationPolicy(require_evidence_trace=True),
    )
    assert validate_mutation_bundle_shape(bundle).ok

    transaction = registry.begin_mutation(handle.id, "v1").unwrap()
    candidate = transaction.apply(handle).unwrap()
    assert candidate.version == "v2"
    transaction.commit()
    assert registry.active(handle.id).unwrap().version == "v2"

    decision = decide_evolution(
        (
            make_trace(metadata={"gap": "permission ownership unknown"}),
            make_trace(metadata={"gap": "permission ownership unknown"}),
        )
    )
    assert decision.level == 1
    assert decision.proposed_mutation_kinds == ("context",)

    evaluator = DefaultEvolutionEvaluator(min_delta=0)
    before = evaluator.score_run((make_trace(outcome="fail"),))
    after = evaluator.score_run((make_trace(outcome="pass"),))
    assert evaluator.compare(before, after, bundle).accepted


def test_loop_mutation_graph_validation_structure_mutation_and_shadow_eval():
    handle = create(make_minimal_counter_loop()).unwrap()
    implementations = InMemoryImplementationRegistry()
    implementations.register("same-step", handle.definition.step)
    mutation = LoopMutation(step_ref="same-step")
    mutated = apply_loop_mutation(handle.definition, mutation, implementations).unwrap()
    assert mutated.version == "v2"
    assert mutated.step is handle.definition.step

    graph = CompositionGraph(
        version="v1",
        nodes=(
            CompositionNode("plan", handle.id),
            CompositionNode("execute", handle.id),
        ),
        edges=(CompositionEdge("plan", "execute", "chain"),),
    )
    assert validate_composition_graph(graph).ok
    patched = apply_structure_mutation(
        graph,
        StructureMutation(operation="insert-loop", loop_ref="validate", insert_after="execute"),
    ).unwrap()
    assert patched.version == "v2"
    assert any(node.id == "validate" for node in patched.nodes)

    async def scenario():
        result = await run_shadow_evaluation(handle, create(mutated).unwrap(), (make_initial_counter_context(1),))
        assert result.before.metrics["runs"] == 1
        assert result.after.metrics["runs"] == 1
        assert result.evaluation.accepted

    asyncio.run(scenario())


def make_trace(outcome="pass", metadata=None):
    trace_id = new_trace_id()
    return Trace(
        id=trace_id,
        run_id=new_run_id(),
        loop_id=new_loop_id(),
        loop_version=new_loop_version(),
        step_number=as_step_number(0),
        root_trace_id=trace_id,
        started_at=NOW,
        ended_at=NOW,
        duration_ms=1,
        input_context_id="ctx-in",
        output_context_id="ctx-out",
        outcome=outcome,
        error=make_loom_error("LOOP_FAILED", "failed", retryable=False) if outcome == "fail" else None,
        metadata=metadata,
    )
