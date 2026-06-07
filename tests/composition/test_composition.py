import asyncio
from dataclasses import replace

from loom.composition import (
    chain,
    fork,
    nest,
)
from loom.core import (
    Context,
    GoalLayer,
    IdentityLayer,
    MinimalLoopDefinition,
    Observation,
    StateLayer,
    StepResult,
    Trace,
    as_step_number,
    empty_affordances,
    empty_knowledge,
    empty_state,
    err,
    make_loom_error,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    ok,
)
from loom.runtime import (
    create,
    step,
)

NOW = "2026-06-04T00:00:00.000Z"


def test_chain_runs_children_in_order_and_fail_fast():
    async def scenario():
        context = make_context()
        first = create(make_append_loop("first")).unwrap()
        second = create(make_append_loop("second")).unwrap()
        chained = chain((first, second)).unwrap()
        result = (await step(chained, context)).unwrap()

        assert [obs.source for obs in result.context.state.observations] == ["first", "second"]
        assert result.trace.metadata["composition"] == "chain"
        assert len(result.trace.children) == 2

        calls = []
        failing = create(make_failing_loop()).unwrap()
        after = create(make_append_loop("after", calls=calls)).unwrap()
        failed_chain = chain((failing, after)).unwrap()
        failed = await step(failed_chain, make_context())
        assert not failed.ok
        assert failed.error.code == "LOOP_FAILED"
        assert calls == []

    asyncio.run(scenario())


def test_nest_projects_inner_and_merges_child_output():
    async def scenario():
        parent = create(make_append_loop("parent")).unwrap()
        child = create(make_append_loop("child")).unwrap()
        nested = nest(parent, child).unwrap()

        result = (await step(nested, make_context())).unwrap()
        assert any(obs.source == "parent" for obs in result.context.state.observations)
        assert any(obs.source == "child" for obs in result.context.state.observations)
        assert result.trace.metadata["composition"] == "nest"
        assert len(result.trace.children) >= 2

    asyncio.run(scenario())


def test_fork_runs_slices_with_bounded_concurrency_and_quorum():
    async def scenario():
        worker = create(make_append_loop("worker")).unwrap()

        async def split(_context):
            return ("a", "b", "c")

        forked = fork(worker, split=split, concurrency=2).unwrap()
        result = (await step(forked, make_context())).unwrap()
        assert result.trace.metadata["composition"] == "fork"
        assert result.trace.metadata["maxObservedConcurrency"] <= 2
        assert len([obs for obs in result.context.state.observations if obs.source == "worker"]) == 3

        failing_worker = create(make_failing_loop()).unwrap()
        quorum_fork = fork(failing_worker, split=split, error_mode="quorum", quorum=1).unwrap()
        failed = await step(quorum_fork, make_context())
        assert not failed.ok
        assert failed.error.code == "LOOP_FAILED"
        assert failed.error.metadata["required"] == 1
        assert failed.error.metadata["actual"] == 0

    asyncio.run(scenario())


def make_context():
    return Context(
        id=new_context_id(),
        run_id=new_run_id(),
        created_at=NOW,
        identity=IdentityLayer(role="composition test"),
        goal=GoalLayer(objective="Compose loops"),
        state=empty_state(),
        knowledge=empty_knowledge(),
        affordances=empty_affordances(),
    )


def make_append_loop(source, calls=None):
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context, _runtime):
        if calls is not None:
            calls.append(source)
        observation = Observation(f"{source}-{len(context.state.observations) + 1}", source, {"ok": True}, NOW)
        next_context = replace(
            context,
            id=new_context_id(),
            state=StateLayer(observations=(*context.state.observations, observation)),
        )
        trace_id = new_trace_id()
        return ok(
            StepResult(
                next_context,
                Trace(
                    id=trace_id,
                    run_id=context.run_id,
                    loop_id=loop_id,
                    loop_version=version,
                    step_number=as_step_number(len(context.state.observations)),
                    root_trace_id=trace_id,
                    started_at=NOW,
                    ended_at=NOW,
                    duration_ms=1,
                    input_context_id=context.id,
                    output_context_id=next_context.id,
                    outcome="pass",
                    observations=(observation,),
                ),
            )
        )

    return MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role=f"{source} loop"),
        goal=GoalLayer(objective=f"Append {source}"),
        step=step_fn,
        done=lambda context, _runtime: ok(False),
    )


def make_failing_loop():
    loop_id = new_loop_id()
    version = new_loop_version()
    return MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role="failing loop"),
        goal=GoalLayer(objective="Fail"),
        step=lambda _context, _runtime: err(make_loom_error("LOOP_FAILED", "failed", retryable=False)),
        done=lambda _context, _runtime: ok(False),
    )
