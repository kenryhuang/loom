"""
Complex project-level test: Multi-stage composition pipeline.

Tests chain + nest + fork composition operators working together in a
realistic data processing pipeline scenario: retrieve → parallel review →
summarize → nested validation.

Exercises: composition, runtime, core models, observability (trace tree).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from loom.composition import (
    chain,
    fork,
    nest,
)
from loom.core import (
    Action,
    Context,
    Decision,
    GoalLayer,
    IdentityLayer,
    MinimalLoopDefinition,
    Observation,
    StepResult,
    Trace,
    as_step_number,
    empty_affordances,
    empty_knowledge,
    empty_state,
    err,
    freeze_context,
    make_loom_error,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    now_iso,
    ok,
)
from loom.runtime import create
from loom.runtime import step as runtime_step


def _make_stage_loop(stage_name: str, fail_on: set[str] | None = None):
    """Create a loop that appends a stage observation; optionally fails."""
    loop_id = new_loop_id()
    version = new_loop_version()
    fail_set = fail_on or set()

    async def step_fn(context: Context, _runtime: Any):
        if stage_name in fail_set:
            return err(make_loom_error("TOOL_FAILED", f"Stage {stage_name} failed", retryable=False))

        counter = len(context.state.observations) + 1
        at = now_iso()
        observation = Observation(
            f"{stage_name}-{counter}",
            stage_name,
            {"stage": stage_name, "counter": counter},
            at,
        )
        action = Action(f"action-{stage_name}", "custom", f"Run {stage_name}")
        decision = Decision(f"decision-{stage_name}", action, f"Execute {stage_name}", (), 1.0, at)
        next_context = freeze_context(
            replace(
                context,
                id=new_context_id(),
                state=replace(
                    context.state,
                    observations=(*context.state.observations, observation),
                    decisions=(*context.state.decisions, decision),
                ),
            )
        )
        trace_id = new_trace_id()
        trace = Trace(
            id=trace_id,
            run_id=context.run_id,
            loop_id=loop_id,
            loop_version=version,
            step_number=as_step_number(len(context.state.observations)),
            root_trace_id=trace_id,
            started_at=at,
            ended_at=now_iso(),
            duration_ms=1,
            input_context_id=context.id,
            output_context_id=next_context.id,
            outcome="pass",
            observations=(observation,),
            decisions=(decision,),
            actions=(action,),
            tags=("composition-test", stage_name),
        )
        return ok(StepResult(next_context, trace, observation, {"stage": stage_name}))

    return MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role=f"{stage_name} stage"),
        goal=GoalLayer(objective=f"Execute {stage_name}"),
        step=step_fn,
        done=lambda _ctx, _rt: ok(False),
    )


def _make_pipeline_context():
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=now_iso(),
            identity=IdentityLayer(role="pipeline executor"),
            goal=GoalLayer(objective="Run multi-stage pipeline", budget={"max_steps": 10}),
            state=empty_state(),
            knowledge=empty_knowledge(),
            affordances=empty_affordances(),
        )
    )


@pytest.mark.asyncio
async def test_chain_fork_nest_full_pipeline():
    """
    Scenario: A realistic data processing pipeline.
    1. Chain: retrieve → transform
    2. Fork: parallel review on 3 slices
    3. Nest: parent(validate) → child(annotate)
    4. Final chain of all stages
    """
    # Stage 1: Chain of retrieve → transform
    retrieve_loop = create(_make_stage_loop("retrieve")).unwrap()
    transform_loop = create(_make_stage_loop("transform")).unwrap()
    chain_handle = chain((retrieve_loop, transform_loop)).unwrap()

    ctx = _make_pipeline_context()
    chain_result = await runtime_step(chain_handle, ctx)
    assert chain_result.ok
    stages = [obs.source for obs in chain_result.value.context.state.observations]
    assert "retrieve" in stages
    assert "transform" in stages

    # Stage 2: Fork parallel review over 3 items
    review_loop = create(_make_stage_loop("review")).unwrap()

    async def split_context(_context):
        return ("item_a", "item_b", "item_c")

    fork_handle = fork(review_loop, split=split_context, concurrency=3).unwrap()
    fork_result = await runtime_step(fork_handle, chain_result.value.context)
    assert fork_result.ok
    review_obs = [obs for obs in fork_result.value.context.state.observations if obs.source == "review"]
    assert len(review_obs) == 3

    # Stage 3: Nest validate → annotate
    validate_loop = create(_make_stage_loop("validate")).unwrap()
    annotate_loop = create(_make_stage_loop("annotate")).unwrap()
    nest_handle = nest(validate_loop, annotate_loop).unwrap()
    nest_result = await runtime_step(nest_handle, fork_result.value.context)
    assert nest_result.ok
    nest_stages = [obs.source for obs in nest_result.value.context.state.observations]
    assert "validate" in nest_stages
    assert "annotate" in nest_stages

    # Stage 4: Full pipeline chain
    full_chain = chain((retrieve_loop, transform_loop, review_loop)).unwrap()
    full_result = await runtime_step(full_chain, _make_pipeline_context())
    assert full_result.ok
    all_stages = [obs.source for obs in full_result.value.context.state.observations]
    assert all_stages == ["retrieve", "transform", "review"]


@pytest.mark.asyncio
async def test_chain_fail_fast_propagates_error():
    """When a stage in a chain fails, the entire chain returns error."""
    stage_a = create(_make_stage_loop("stage_a")).unwrap()
    stage_b = create(_make_stage_loop("stage_b", fail_on={"stage_b"})).unwrap()
    stage_c = create(_make_stage_loop("stage_c")).unwrap()

    chain_handle = chain((stage_a, stage_b, stage_c)).unwrap()
    result = await runtime_step(chain_handle, _make_pipeline_context())

    assert not result.ok
    assert result.error.code == "TOOL_FAILED"
    assert "stage_b" in result.error.message


@pytest.mark.asyncio
async def test_fork_collect_errors_mode():
    """Fork with collect-errors mode: some slices fail, results collected."""
    worker = create(_make_stage_loop("worker", fail_on={"worker"})).unwrap()

    async def split(_context):
        return ("slice_1", "slice_2")

    fork_handle = fork(worker, split=split, concurrency=2, error_mode="collect-errors").unwrap()
    result = await runtime_step(fork_handle, _make_pipeline_context())

    # All slices fail → no successful results, but collect-errors doesn't fail-fast
    # The fork still runs but has 0 successful results
    assert result.ok  # collect-errors mode doesn't fail


@pytest.mark.asyncio
async def test_fork_quorum_not_met():
    """Fork with quorum mode: not enough successful slices."""
    worker = create(_make_stage_loop("quorum_worker", fail_on={"quorum_worker"})).unwrap()

    async def split(_context):
        return ("a", "b", "c")

    fork_handle = fork(worker, split=split, concurrency=3, error_mode="quorum", quorum=3).unwrap()
    result = await runtime_step(fork_handle, _make_pipeline_context())

    assert not result.ok
    assert result.error.code == "LOOP_FAILED"
    assert "quorum" in result.error.message.lower()


@pytest.mark.asyncio
async def test_deeply_nested_composition_three_levels():
    """Three-level nesting: grandparent → parent → child."""
    grandparent = create(_make_stage_loop("grandparent")).unwrap()
    parent = create(_make_stage_loop("parent")).unwrap()
    child = create(_make_stage_loop("child")).unwrap()

    inner_nest = nest(parent, child).unwrap()
    outer_nest = nest(grandparent, inner_nest).unwrap()

    result = await runtime_step(outer_nest, _make_pipeline_context())
    assert result.ok
    sources = [obs.source for obs in result.value.context.state.observations]
    assert "grandparent" in sources
    assert "parent" in sources
    assert "child" in sources


@pytest.mark.asyncio
async def test_composition_trace_tree_structure():
    """Verify that composition operators produce queryable trace trees."""
    from loom.observability import DefaultTraceReader, InMemoryTraceStore

    store = InMemoryTraceStore()
    retrieve_loop = create(_make_stage_loop("trace_retrieve"), trace_store=store).unwrap()
    transform_loop = create(_make_stage_loop("trace_transform"), trace_store=store).unwrap()
    chain_handle = chain((retrieve_loop, transform_loop)).unwrap()

    ctx = _make_pipeline_context()
    result = await runtime_step(chain_handle, ctx)
    assert result.ok

    reader = DefaultTraceReader(store)
    summary = await reader.summarize({"run_id": ctx.run_id})
    assert summary["count"] >= 2  # at least retrieve + transform traces
    assert summary["by_outcome"].get("pass", 0) >= 2
