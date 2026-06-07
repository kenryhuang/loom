"""Runtime execution for Loom loops."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from loom.core.models import (
    Context,
    LoopHandle,
    MinimalLoopDefinition,
    Result,
    RunMetrics,
    RunResult,
    StepResult,
    Trace,
    as_step_number,
    err,
    make_loom_error,
    new_trace_id,
    now_iso,
    ok,
    to_loom_error,
)
from loom.observability.traces import (
    DefaultTraceReader,
    InMemoryTraceStore,
    TraceSink,
    create_in_memory_trace_sink,
)

_RUNTIME_STATE: dict[int, RuntimeState] = {}


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class RegistryView:
    def __init__(self, items: Mapping[str, Any] | None = None, missing_message: str = "Not found"):
        self._items = dict(items or {})
        self._missing_message = missing_message

    def get(self, key: str, version: str | None = None) -> Result:
        exact_key = f"{key}@{version}" if version is not None else key
        value = self._items.get(exact_key, self._items.get(key))
        if value is None:
            return err(make_loom_error("VALIDATION_FAILED", self._missing_message, retryable=False))
        return ok(value)


@dataclass(frozen=True, slots=True)
class RuntimeRegistry:
    tools: RegistryView
    loops: RegistryView
    evaluators: RegistryView
    implementations: RegistryView


def create_runtime_registry(
    *,
    tools: Mapping[str, Any] | None = None,
    loops: Mapping[str, Any] | None = None,
    evaluators: Mapping[str, Any] | None = None,
    implementations: Mapping[str, Any] | None = None,
) -> RuntimeRegistry:
    return RuntimeRegistry(
        tools=RegistryView(tools, "Tool not found"),
        loops=RegistryView(loops, "Loop not found"),
        evaluators=RegistryView(evaluators, "Evaluator not found"),
        implementations=RegistryView(implementations, "Implementation not found"),
    )


default_runtime_registry = create_runtime_registry()


@dataclass(frozen=True, slots=True)
class RuntimeState:
    trace_store: InMemoryTraceStore
    trace_sink: TraceSink
    registry: RuntimeRegistry


@dataclass(frozen=True, slots=True)
class StepRuntime:
    run_id: str
    loop_id: str
    cancellation: CancellationToken
    registry: RuntimeRegistry
    trace_sink: Any
    now: Callable[[], str]
    apply_patch: Callable[[Context, Any], Result]
    call_tool: Callable[..., Awaitable[Result]]
    run_loop: Callable[..., Awaitable[Result]]


@dataclass(frozen=True, slots=True)
class DoneRuntime:
    cancellation: CancellationToken
    now: Callable[[], str]
    registry: RuntimeRegistry


def create(
    definition: MinimalLoopDefinition,
    *,
    trace_store: InMemoryTraceStore | None = None,
    registry: RuntimeRegistry | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Result:
    validation = _validate_definition(definition)
    if not validation.ok:
        return validation
    store = trace_store or InMemoryTraceStore()
    state = RuntimeState(
        trace_store=store,
        trace_sink=create_in_memory_trace_sink(store),
        registry=registry or default_runtime_registry,
    )
    handle = LoopHandle(
        id=definition.id,
        version=definition.version,
        definition=definition,
        trace_reader=DefaultTraceReader(store),
        created_at=now_iso(),
    )
    _RUNTIME_STATE[id(handle)] = state
    return ok(handle)


async def step(
    loop: LoopHandle,
    context: Context,
    *,
    cancellation: CancellationToken | None = None,
    timeout_ms: int | None = None,
    trace_sink: Any | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Result:
    state = _RUNTIME_STATE.get(id(loop)) or RuntimeState(InMemoryTraceStore(), create_in_memory_trace_sink(InMemoryTraceStore()), default_runtime_registry)
    token = cancellation or CancellationToken()
    trace_id = new_trace_id()
    started_at = now_iso()
    started_ms = time.monotonic()
    emit = _make_emitter(state.trace_sink, trace_sink)
    started = await emit({"type": "step.started", "trace_id": trace_id, "at": started_at, "context_id": context.id})
    if not started.ok:
        return started

    if token.cancelled:
        error = make_loom_error("ABORTED", "Operation aborted", retryable=False, trace_id=trace_id)
        terminal = _terminal_trace(loop, context, trace_id, started_at, started_ms, "cancelled", error)
        persisted = await _persist_completed(emit, terminal)
        return err(error) if persisted.ok else persisted

    runtime = _make_step_runtime(loop, context, state, token, emit)
    try:
        result = await _maybe_await(loop.definition.step(context, runtime), timeout_ms)
    except BaseException as exc:
        loom_error = _with_trace_id(to_loom_error(exc), trace_id)
        terminal = _terminal_trace(loop, context, trace_id, started_at, started_ms, "fail", loom_error)
        persisted = await _persist_completed(emit, terminal)
        return err(loom_error) if persisted.ok else persisted

    if not isinstance(result, Result):
        result = ok(result)
    if not result.ok:
        loom_error = _with_trace_id(result.error, trace_id)
        terminal = _terminal_trace(loop, context, trace_id, started_at, started_ms, "fail", loom_error)
        persisted = await _persist_completed(emit, terminal)
        return err(loom_error) if persisted.ok else persisted

    step_result = _normalize_step_result(result.value)
    persisted = await _persist_completed(emit, step_result.trace)
    if not persisted.ok:
        return persisted
    return ok(step_result)


async def done(
    loop: LoopHandle,
    context: Context,
    *,
    cancellation: CancellationToken | None = None,
    timeout_ms: int | None = None,
) -> Result:
    token = cancellation or CancellationToken()
    if token.cancelled:
        return err(make_loom_error("ABORTED", "Operation aborted", retryable=False))
    max_steps = context.goal.budget.max_steps
    if max_steps is not None and len(context.state.observations) >= max_steps:
        return ok(True)

    state = _RUNTIME_STATE.get(id(loop))
    registry = state.registry if state else default_runtime_registry
    criteria_result = await _evaluate_required_criteria(context, registry)
    if not criteria_result.ok or criteria_result.value:
        return criteria_result

    try:
        result = await _maybe_await(loop.definition.done(context, DoneRuntime(token, now_iso, registry)), timeout_ms)
    except BaseException as exc:
        return err(to_loom_error(exc))
    return result if isinstance(result, Result) else ok(bool(result))


async def run(
    loop: LoopHandle,
    initial_context: Context,
    *,
    cancellation: CancellationToken | None = None,
    timeout_ms: int | None = None,
    max_steps: int | None = None,
    trace_sink: Any | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Result:
    started_ms = time.monotonic()
    started_at = now_iso()
    current = initial_context
    traces: list[Trace] = []
    output = None
    steps = 0

    while True:
        is_done = await done(loop, current, cancellation=cancellation, timeout_ms=timeout_ms)
        if not is_done.ok:
            return is_done
        if is_done.value:
            ended_at = now_iso()
            return ok(
                RunResult(
                    context=current,
                    traces=tuple(traces),
                    output=output,
                    metrics=RunMetrics(
                        steps=steps,
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=_duration_ms(started_ms),
                        trace_count=len(traces),
                        outcome="pass",
                    ),
                )
            )
        if max_steps is not None and steps >= max_steps:
            return err(make_loom_error("BUDGET_EXCEEDED", "Run max_steps exceeded", retryable=False))
        stepped = await step(
            loop,
            current,
            cancellation=cancellation,
            timeout_ms=timeout_ms,
            trace_sink=trace_sink,
            metadata=metadata,
        )
        if not stepped.ok:
            return stepped
        current = stepped.value.context
        traces.append(stepped.value.trace)
        output = stepped.value.output
        steps += 1


async def step_stream(loop: LoopHandle, context: Context, **options: Any):
    events: list[Mapping[str, Any]] = []

    class Sink:
        async def emit(self, event):
            events.append(event)
            return ok(None)

    await step(loop, context, trace_sink=Sink(), **options)
    for event in events:
        yield event


async def create_promise_pool(
    task_factories: list[Callable[[], Awaitable[Any]]],
    *,
    concurrency: int,
    cancellation: CancellationToken | None = None,
) -> dict[str, Any]:
    token = cancellation or CancellationToken()
    semaphore = asyncio.Semaphore(concurrency)
    running = 0
    max_observed = 0

    async def run_one(index: int, factory: Callable[[], Awaitable[Any]]):
        nonlocal running, max_observed
        if token.cancelled:
            return {"index": index, "ok": False, "error": "cancelled"}
        async with semaphore:
            running += 1
            max_observed = max(max_observed, running)
            try:
                value = await factory()
                return {"index": index, "ok": True, "value": value}
            except BaseException as exc:
                return {"index": index, "ok": False, "error": exc}
            finally:
                running -= 1

    results = await asyncio.gather(*(run_one(index, factory) for index, factory in enumerate(task_factories)))
    return {"results": tuple(results), "max_observed_concurrency": max_observed}


def _validate_definition(definition: MinimalLoopDefinition) -> Result:
    if not definition.id:
        return err(make_loom_error("VALIDATION_FAILED", "Loop definition id is required", retryable=False))
    if not definition.version:
        return err(make_loom_error("VALIDATION_FAILED", "Loop definition version is required", retryable=False))
    if not definition.identity.role:
        return err(make_loom_error("VALIDATION_FAILED", "Loop definition identity.role is required", retryable=False))
    if not definition.goal.objective:
        return err(make_loom_error("VALIDATION_FAILED", "Loop definition goal.objective is required", retryable=False))
    if not callable(definition.step):
        return err(make_loom_error("VALIDATION_FAILED", "Loop definition step must be callable", retryable=False))
    if not callable(definition.done):
        return err(make_loom_error("VALIDATION_FAILED", "Loop definition done must be callable", retryable=False))
    return ok(None)


async def _maybe_await(value: Any, timeout_ms: int | None = None) -> Any:
    if inspect.isawaitable(value):
        if timeout_ms is None:
            return await value
        return await asyncio.wait_for(value, timeout_ms / 1000)
    return value


def _normalize_step_result(value: Any) -> StepResult:
    if isinstance(value, StepResult):
        return value
    if isinstance(value, Mapping):
        return StepResult(
            context=value["context"],
            trace=value["trace"],
            observation=value.get("observation"),
            output=value.get("output"),
        )
    raise TypeError("Step result must be StepResult or mapping")


def _make_step_runtime(
    loop: LoopHandle,
    context: Context,
    state: RuntimeState,
    token: CancellationToken,
    emit: Callable[[Mapping[str, Any]], Awaitable[Result]],
) -> StepRuntime:
    async def call_tool(tool_id: str, input_value: Any, **options: Any) -> Result:
        handler = state.registry.tools.get(tool_id)
        if not handler.ok:
            return handler
        invoke = getattr(handler.value, "invoke", handler.value)
        return await _maybe_await(invoke(input_value, options))

    async def run_loop(loop_id: str, child_context: Context, **options: Any) -> Result:
        child = state.registry.loops.get(loop_id)
        if not child.ok:
            return child
        return await run(child.value, child_context, **options)

    return StepRuntime(
        run_id=context.run_id,
        loop_id=loop.id,
        cancellation=token,
        registry=state.registry,
        trace_sink=type("RuntimeSink", (), {"emit": staticmethod(emit)})(),
        now=now_iso,
        apply_patch=lambda current, _patch: ok(current),
        call_tool=call_tool,
        run_loop=run_loop,
    )


def _make_emitter(primary: Any, secondary: Any | None):
    async def emit(event: Mapping[str, Any]) -> Result:
        primary_result = await primary.emit(event)
        if not primary_result.ok:
            return primary_result
        if secondary is not None and secondary is not primary:
            return await secondary.emit(event)
        return ok(None)

    return emit


async def _persist_completed(emit: Callable[[Mapping[str, Any]], Awaitable[Result]], trace: Trace) -> Result:
    return await emit({"type": "step.completed", "trace": trace, "at": now_iso()})


def _terminal_trace(
    loop: LoopHandle,
    context: Context,
    trace_id: str,
    started_at: str,
    started_ms: float,
    outcome: str,
    error: Any,
) -> Trace:
    return Trace(
        id=trace_id,
        run_id=context.run_id,
        loop_id=loop.id,
        loop_version=loop.version,
        step_number=as_step_number(len(context.state.observations)),
        root_trace_id=trace_id,
        started_at=started_at,
        ended_at=now_iso(),
        duration_ms=_duration_ms(started_ms),
        input_context_id=context.id,
        output_context_id=context.id,
        outcome=outcome,
        error=error,
    )


def _with_trace_id(error: Any, trace_id: str):
    if error.trace_id is not None:
        return error
    return make_loom_error(
        error.code,
        error.message,
        retryable=error.retryable,
        trace_id=trace_id,
        cause=error.cause,
        metadata=error.metadata,
    )


async def _evaluate_required_criteria(context: Context, registry: RuntimeRegistry) -> Result:
    criteria = [criterion for criterion in context.goal.criteria if criterion.required and criterion.evaluator is not None]
    if not criteria:
        return ok(False)
    for criterion in criteria:
        evaluator = registry.evaluators.get(criterion.evaluator)
        if not evaluator.ok:
            return evaluator
        evaluate = getattr(evaluator.value, "evaluate", evaluator.value)
        passed = await _maybe_await(evaluate(context, criterion))
        if not isinstance(passed, Result):
            passed = ok(bool(passed))
        if not passed.ok or not passed.value:
            return passed
    return ok(True)


def _duration_ms(started_ms: float) -> int:
    return max(0, int((time.monotonic() - started_ms) * 1000))


__all__ = [
    "CancellationToken",
    "DoneRuntime",
    "RegistryView",
    "RuntimeRegistry",
    "RuntimeState",
    "StepRuntime",
    "create",
    "create_promise_pool",
    "create_runtime_registry",
    "default_runtime_registry",
    "done",
    "run",
    "step",
    "step_stream",
]
