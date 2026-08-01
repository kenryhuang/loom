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
    err,
    freeze_context,
    make_loom_error,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
    ok,
)
from loom.observability import EventRecordingPolicy, InMemoryTraceStore
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


def test_run_emits_run_step_and_tool_events_to_trace_store():
    async def scenario():
        class SearchTool:
            async def invoke(self, input_value, options):
                return ok(Observation("tool-obs", "search", {"hit": input_value["query"], "options": options}, NOW))

        loop_id = new_loop_id()
        version = new_loop_version()

        async def step_fn(context, runtime):
            tool_result = await runtime.call_tool("search", {"query": "loom"}, metadata={"tool_call_id": "call_search"})
            if not tool_result.ok:
                return tool_result
            next_context = freeze_context(
                replace(
                    context,
                    id=new_context_id(),
                    state=StateLayer(observations=(*context.state.observations, tool_result.value)),
                )
            )
            return ok(StepResult(next_context, make_trace(context, loop_id, version, next_context)))

        definition = MinimalLoopDefinition(
            id=loop_id,
            version=version,
            identity=IdentityLayer(role="tool loop"),
            goal=GoalLayer(objective="Call a tool"),
            step=step_fn,
            done=lambda context, _runtime: ok(len(context.state.observations) >= 1),
        )
        store = InMemoryTraceStore()
        handle = create(definition, trace_store=store, registry=create_runtime_registry(tools={"search": SearchTool()})).unwrap()

        result = await run(handle, make_context())

        assert result.ok
        event_types = [event["type"] for event in store.events()]
        assert event_types == [
            "run.started",
            "step.started",
            "tool.started",
            "tool.completed",
            "action.recorded",
            "step.completed",
            "run.completed",
        ]
        tool_started = store.events()[2]
        tool_completed = store.events()[3]
        assert tool_started["tool_id"] == "search"
        assert tool_started["input"] == {"query": "loom"}
        assert tool_started["metadata"]["tool_call_id"] == "call_search"
        assert tool_completed["output"].value["hit"] == "loom"
        assert store.events()[-1]["steps"] == 1

    asyncio.run(scenario())


def test_runtime_marks_handler_failure_as_tool_domain():
    async def scenario():
        captured = []

        async def broken_tool(_input, _options):
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "old_text missing",
                    retryable=False,
                    metadata={"edit_index": 4},
                )
            )

        async def step_fn(_context, runtime):
            result = await runtime.call_tool("edit_file", {"path": "sample.py"})
            captured.append(result)
            return result

        definition = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="runtime failure test"),
            goal=GoalLayer(objective="classify a failed tool"),
            step=step_fn,
            done=lambda _context, _runtime: ok(False),
        )
        store = InMemoryTraceStore()
        handle = create(
            definition,
            trace_store=store,
            registry=create_runtime_registry(tools={"edit_file": broken_tool}),
        ).unwrap()

        result = await run(handle, make_context(), max_steps=1)

        assert not result.ok
        assert captured[0].error.metadata == {"edit_index": 4, "failureDomain": "tool"}
        assert [event["type"] for event in store.events()].count("tool.failed") == 1

    asyncio.run(scenario())


def test_runtime_marks_missing_handler_as_tool_domain():
    async def scenario():
        captured = []

        async def step_fn(_context, runtime):
            result = await runtime.call_tool("missing_tool", {})
            captured.append(result)
            return result

        definition = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="missing tool test"),
            goal=GoalLayer(objective="classify a missing tool"),
            step=step_fn,
            done=lambda _context, _runtime: ok(False),
        )
        handle = create(definition, registry=create_runtime_registry()).unwrap()

        result = await run(handle, make_context(), max_steps=1)

        assert not result.ok
        assert captured[0].error.code == "VALIDATION_FAILED"
        assert captured[0].error.metadata["failureDomain"] == "tool"

    asyncio.run(scenario())


def test_runtime_does_not_mark_aborted_handler_failure_as_tool_domain():
    async def scenario():
        captured = []

        async def aborted_tool(_input, _options):
            return err(make_loom_error("ABORTED", "cancelled", retryable=False))

        async def step_fn(_context, runtime):
            result = await runtime.call_tool("cancel", {})
            captured.append(result)
            return result

        definition = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="aborted tool test"),
            goal=GoalLayer(objective="preserve cancellation"),
            step=step_fn,
            done=lambda _context, _runtime: ok(False),
        )
        handle = create(definition, registry=create_runtime_registry(tools={"cancel": aborted_tool})).unwrap()

        result = await run(handle, make_context(), max_steps=1)

        assert not result.ok
        assert captured[0].error.code == "ABORTED"
        assert "failureDomain" not in (captured[0].error.metadata or {})

    asyncio.run(scenario())


def test_runtime_does_not_reclassify_trace_sink_failure_as_tool_domain():
    async def scenario():
        captured = []
        called = False

        class FailingRecorder:
            async def emit(self, event):
                if event["type"] == "tool.started":
                    return err(make_loom_error("TRACE_WRITE_FAILED", "trace unavailable", retryable=False))
                return ok(None)

        async def tool(_input, _options):
            nonlocal called
            called = True
            return ok(Observation("obs", "tool", {"ok": True}, NOW))

        async def step_fn(_context, runtime):
            result = await runtime.call_tool("read_file", {})
            captured.append(result)
            return result

        definition = MinimalLoopDefinition(
            id=new_loop_id(),
            version=new_loop_version(),
            identity=IdentityLayer(role="trace failure test"),
            goal=GoalLayer(objective="preserve trace failures"),
            step=step_fn,
            done=lambda _context, _runtime: ok(False),
        )
        handle = create(
            definition,
            event_recorder=FailingRecorder(),
            registry=create_runtime_registry(tools={"read_file": tool}),
        ).unwrap()

        result = await run(handle, make_context(), max_steps=1)

        assert not result.ok
        assert not called
        assert captured[0].error.code == "TRACE_WRITE_FAILED"
        assert "failureDomain" not in (captured[0].error.metadata or {})

    asyncio.run(scenario())


def test_create_accepts_pluggable_event_recorder():
    async def scenario():
        captured = []

        class Recorder:
            async def emit(self, event):
                captured.append(event)
                return ok(None)

        handle = create(make_one_step_definition(), event_recorder=Recorder()).unwrap()

        result = await run(handle, make_context())

        assert result.ok
        assert [event["type"] for event in captured] == [
            "run.started",
            "step.started",
            "action.recorded",
            "step.completed",
            "run.completed",
        ]

    asyncio.run(scenario())


def test_event_recording_can_be_disabled_while_traces_remain_queryable():
    async def scenario():
        store = InMemoryTraceStore()
        handle = create(make_one_step_definition(), trace_store=store, event_policy=EventRecordingPolicy(enabled=False)).unwrap()

        result = await run(handle, make_context())

        assert result.ok
        assert store.events() == ()
        traces = [trace async for trace in handle.trace_reader.query({"run_id": result.value.context.run_id})]
        assert len(traces) == 1

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
