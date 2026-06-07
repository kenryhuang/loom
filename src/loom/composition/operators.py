"""Loop composition helpers."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any

from loom.core.models import (
    Context,
    GoalLayer,
    IdentityLayer,
    MinimalLoopDefinition,
    StateLayer,
    StepResult,
    Trace,
    as_step_number,
    emit_child_output,
    err,
    make_loom_error,
    merge_child_output,
    new_context_id,
    new_loop_id,
    new_loop_version,
    new_trace_id,
    ok,
    project,
)
from loom.runtime.engine import create, create_promise_pool, step


def chain(loops: tuple[Any, ...], *, error_mode: str = "fail-fast"):
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context: Context, _runtime: Any):
        current = context
        child_traces = []
        output = None
        for child in loops:
            result = await step(child, current)
            if not result.ok:
                return result
            child_traces.append(result.value.trace)
            current = result.value.context
            output = result.value.output
        trace_id = new_trace_id()
        trace = Trace(
            id=trace_id,
            run_id=context.run_id,
            loop_id=loop_id,
            loop_version=version,
            step_number=as_step_number(len(context.state.observations)),
            root_trace_id=trace_id,
            started_at=child_traces[0].started_at if child_traces else _now(),
            ended_at=child_traces[-1].ended_at if child_traces else _now(),
            duration_ms=sum(trace.duration_ms for trace in child_traces),
            input_context_id=context.id,
            output_context_id=current.id,
            outcome="pass",
            observations=current.state.observations[len(context.state.observations) :],
            children=tuple(trace.id for trace in child_traces),
            tags=("composition", "chain"),
            metadata={"composition": "chain"},
        )
        return ok(StepResult(current, trace, output=output))

    definition = MinimalLoopDefinition(
        id=loop_id,
        version=version,
        identity=IdentityLayer(role="chain composition"),
        goal=GoalLayer(objective="Run loops sequentially"),
        step=step_fn,
        done=lambda _context, _runtime: ok(False),
    )
    return create(definition)


def nest(parent: Any, child: Any):
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context: Context, _runtime: Any):
        parent_result = await step(parent, context)
        if not parent_result.ok:
            return parent_result
        child_context = project(
            parent_result.value.context,
            child.definition.goal,
            identity=child.definition.identity,
        ).unwrap()
        child_result = await step(child, child_context)
        if not child_result.ok:
            return child_result
        child_output = emit_child_output(child_result.value.context, status="completed")
        merged = merge_child_output(parent_result.value.context, child_output)
        if not merged.ok:
            return merged
        trace_id = new_trace_id()
        trace = Trace(
            id=trace_id,
            run_id=context.run_id,
            loop_id=loop_id,
            loop_version=version,
            step_number=as_step_number(len(context.state.observations)),
            root_trace_id=trace_id,
            started_at=parent_result.value.trace.started_at,
            ended_at=child_result.value.trace.ended_at,
            duration_ms=parent_result.value.trace.duration_ms + child_result.value.trace.duration_ms,
            input_context_id=context.id,
            output_context_id=merged.value.id,
            outcome="pass",
            observations=merged.value.state.observations[len(context.state.observations) :],
            children=(parent_result.value.trace.id, child_result.value.trace.id),
            tags=("composition", "nest"),
            metadata={"composition": "nest"},
        )
        return ok(StepResult(merged.value, trace))

    return create(
        MinimalLoopDefinition(
            id=loop_id,
            version=version,
            identity=IdentityLayer(role="nest composition"),
            goal=GoalLayer(objective="Run parent and child loops"),
            step=step_fn,
            done=lambda _context, _runtime: ok(False),
        )
    )


def fork(
    worker: Any,
    *,
    split,
    concurrency: int = 4,
    error_mode: str = "collect-errors",
    quorum: int | None = None,
):
    loop_id = new_loop_id()
    version = new_loop_version()

    async def step_fn(context: Context, _runtime: Any):
        slices = await _maybe_await(split(context))

        async def run_slice(index: int, slice_value: Any):
            child_context = replace(
                context,
                id=new_context_id(),
                state=StateLayer(),
                metadata={"forkIndex": index, "slice": slice_value},
            )
            return await step(worker, child_context)

        pool = await create_promise_pool(
            [lambda index=index, item=item: run_slice(index, item) for index, item in enumerate(slices)],
            concurrency=concurrency,
        )
        successful = []
        errors = []
        for item in pool["results"]:
            if item["ok"] and item["value"].ok:
                successful.append(item["value"].value)
            else:
                value = item.get("value")
                errors.append(value.error if hasattr(value, "error") else item.get("error"))

        if error_mode == "fail-fast" and errors:
            first = errors[0]
            return err(first if hasattr(first, "code") else make_loom_error("LOOP_FAILED", str(first), retryable=False))

        required = quorum if quorum is not None else (len(slices) if error_mode == "quorum" else 0)
        if error_mode == "quorum" and len(successful) < required:
            return err(
                make_loom_error(
                    "LOOP_FAILED",
                    "Fork quorum not met",
                    retryable=False,
                    metadata={"required": required, "actual": len(successful)},
                )
            )

        observations = []
        for result in successful:
            observations.extend(result.context.state.observations)
        next_context = replace(
            context,
            id=new_context_id(),
            state=replace(
                context.state,
                observations=(*context.state.observations, *observations),
            ),
        )
        trace_id = new_trace_id()
        trace = Trace(
            id=trace_id,
            run_id=context.run_id,
            loop_id=loop_id,
            loop_version=version,
            step_number=as_step_number(len(context.state.observations)),
            root_trace_id=trace_id,
            started_at=_now(),
            ended_at=_now(),
            duration_ms=0,
            input_context_id=context.id,
            output_context_id=next_context.id,
            outcome="pass",
            observations=tuple(observations),
            children=tuple(result.trace.id for result in successful),
            tags=("composition", "fork"),
            metadata={
                "composition": "fork",
                "maxObservedConcurrency": pool["max_observed_concurrency"],
                "errorCount": len(errors),
            },
        )
        return ok(StepResult(next_context, trace))

    return create(
        MinimalLoopDefinition(
            id=loop_id,
            version=version,
            identity=IdentityLayer(role="fork composition"),
            goal=GoalLayer(objective="Run worker loop over slices"),
            step=step_fn,
            done=lambda _context, _runtime: ok(False),
        )
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _now() -> str:
    return "2026-06-04T00:00:00.000Z"


__all__ = ["chain", "fork", "nest"]
