import asyncio
from dataclasses import replace

from loom.core import (
    Action,
    Context,
    GoalLayer,
    IdentityLayer,
    MinimalLoopDefinition,
    Observation,
    StateLayer,
    StepResult,
    SuccessCriterion,
    Trace,
    as_step_number,
    empty_affordances,
    empty_knowledge,
    empty_state,
    freeze_context,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    ok,
)
from loom.runtime import (
    CancellationToken,
    create,
    create_runtime_registry,
    done,
    run,
    step,
)

NOW = "2026-06-04T00:00:00.000Z"


def test_create_step_done_and_run_one_step_loop():
    async def scenario():
        context = make_context(max_steps=1)
        handle = create(make_one_step_definition()).unwrap()

        stepped = (await step(handle, context)).unwrap()
        assert len(stepped.context.state.observations) == 1
        assert stepped.trace.outcome == "pass"
        assert await done(handle, stepped.context) == ok(True)
        assert await handle.trace_reader.get(stepped.trace.id) == ok(stepped.trace)

        run_result = (await run(create(make_one_step_definition()).unwrap(), context)).unwrap()
        assert len(run_result.context.state.observations) == 1
        assert len(run_result.traces) == 1
        assert run_result.metrics.steps == 1
        assert run_result.metrics.outcome == "pass"

    asyncio.run(scenario())


def test_done_uses_required_evaluator_and_budget():
    async def scenario():
        class AlwaysPass:
            async def evaluate(self, context, criterion, options=None):
                return ok(True)

        registry = create_runtime_registry(evaluators={"always-pass": AlwaysPass()})
        context = freeze_context(
            replace(
                make_context(max_steps=10),
                goal=GoalLayer(
                    objective="Use evaluator",
                    criteria=(
                        SuccessCriterion(
                            "criterion",
                            "Always passes",
                            evaluator="always-pass",
                            required=True,
                        ),
                    ),
                ),
            )
        )
        handle = create(make_one_step_definition(), registry=registry).unwrap()

        assert await done(handle, context) == ok(True)
        assert await done(handle, make_context(max_steps=0)) == ok(True)

    asyncio.run(scenario())


def test_step_converts_errors_and_honors_cancellation():
    async def scenario():
        throwing = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="thrower"),
            goal=GoalLayer(objective="Throw"),
            step=lambda _context, _runtime: (_ for _ in ()).throw(ValueError("boom")),
            done=lambda _context, _runtime: ok(False),
        )
        handle = create(throwing).unwrap()
        failed = await step(handle, make_context())
        assert failed.ok is False
        assert failed.error.code == "INTERNAL"
        fail_traces = [trace async for trace in handle.trace_reader.query({"outcome": ("fail",)})]
        assert len(fail_traces) == 1
        assert fail_traces[0].error.code == "INTERNAL"

        calls = 0

        async def counted_step(input_context, _runtime):
            nonlocal calls
            calls += 1
            return ok(StepResult(input_context, make_trace(input_context, throwing.id, throwing.version)))

        abortable = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="abortable"),
            goal=GoalLayer(objective="Abort"),
            step=counted_step,
            done=lambda _context, _runtime: ok(False),
        )
        abortable_handle = create(abortable).unwrap()
        token = CancellationToken()
        token.cancel()
        cancelled = await step(abortable_handle, make_context(), cancellation=token)

        assert calls == 0
        assert cancelled.ok is False
        assert cancelled.error.code == "ABORTED"
        cancelled_traces = [trace async for trace in abortable_handle.trace_reader.query({"outcome": ("cancelled",)})]
        assert len(cancelled_traces) == 1

    asyncio.run(scenario())


def make_context(max_steps=1):
    return freeze_context(
        Context(
            id=new_context_id(),
            run_id=new_run_id(),
            created_at=NOW,
            identity=IdentityLayer(role="runtime test"),
            goal=GoalLayer(objective="Run a minimal loop", budget={"max_steps": max_steps}),
            state=empty_state(),
            knowledge=empty_knowledge(),
            affordances=empty_affordances(),
        )
    )


def make_one_step_definition():
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context, _runtime):
        count = len(context.state.observations) + 1
        observation = Observation(f"obs-{count}", "runtime-test", {"count": count}, NOW)
        next_context = freeze_context(
            replace(
                context,
                id=new_context_id(),
                state=StateLayer(observations=(*context.state.observations, observation)),
            )
        )
        return ok(StepResult(next_context, make_trace(context, loop_id, version, next_context)))

    return MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role="one-step loop"),
        goal=GoalLayer(objective="Append one observation"),
        step=step_fn,
        done=lambda context, _runtime: ok(len(context.state.observations) >= 1),
    )


def make_trace(context, loop_id, version, output_context=None):
    trace_id = new_trace_id()
    output_context = output_context or context
    return Trace(
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
        output_context_id=output_context.id,
        outcome="pass",
        observations=output_context.state.observations,
        actions=(Action("action", "custom", "Act"),),
        tags=("runtime",),
    )
