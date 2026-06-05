import { makeLoomError, toLoomError, type LoomError } from "../core/errors.js";
import {
  asStepNumber,
  newTraceId,
  type DurationMs,
  type ISODateTime,
  type LoopId,
  type LoopVersion,
} from "../core/ids.js";
import { freezeContext, type AnyContext, type Observation } from "../core/context.js";
import type {
  LoopHandle,
  RunOptions,
  StepOptions,
  StepResult,
  StepRuntime,
} from "../core/loop.js";
import type { JsonValue } from "../core/json.js";
import { err, ok, type Result } from "../core/result.js";
import type { Trace, TraceEvent, TraceSink } from "../core/trace.js";
import { composeAbort } from "./cancellation.js";
import {
  defaultRuntimeRegistry,
  getRuntimeState,
  type RuntimeState,
} from "./registry.js";

export async function step<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(
  loop: LoopHandle<TContext, TObservation, TOutput>,
  context: TContext,
  options: StepOptions = {},
): Promise<Result<StepResult<TContext, TObservation, TOutput>>> {
  const state = getRuntimeState(loop) ?? makeFallbackState();
  const abort = composeAbort(options.signal, options.timeoutMs);
  const startedAtMs = Date.now();
  const startedAt = now();
  const traceId = newTraceId();
  const emit = makeEmitter(state.traceSink, options.traceSink);

  try {
    const started = await emit({
      type: "step.started",
      traceId,
      at: startedAt,
      contextId: context.id,
    });
    if (!started.ok) {
      return err(started.error);
    }

    if (abort.signal.aborted) {
      const error = abortError(abort.isTimeout(), traceId);
      const trace = makeTerminalTrace(loop, context, traceId, startedAt, startedAtMs, "cancelled", error);
      const persisted = await persistCompleted(emit, trace);
      return persisted.ok ? err(error) : err(persisted.error);
    }

    const runtime = makeStepRuntime(loop, context, state, abort.signal, emit);
    const result = await loop.definition.step(context, runtime);

    if (!result.ok) {
      const error = withTraceId(result.error, traceId);
      const trace = makeTerminalTrace(loop, context, traceId, startedAt, startedAtMs, "fail", error);
      const persisted = await persistCompleted(emit, trace);
      return persisted.ok ? err(error) : err(persisted.error);
    }

    const frozenContext = freezeContext(result.value.context);
    const frozenTrace = result.value.trace;
    const persisted = await persistCompleted(emit, frozenTrace);
    if (!persisted.ok) {
      return err(persisted.error);
    }

    return ok({
      ...result.value,
      context: frozenContext,
      trace: frozenTrace,
    });
  } catch (error) {
    const loomError = withTraceId(toLoomError(error), traceId);
    const trace = makeTerminalTrace(loop, context, traceId, startedAt, startedAtMs, "fail", loomError);
    const persisted = await persistCompleted(emit, trace);
    return persisted.ok ? err(loomError) : err(persisted.error);
  } finally {
    abort.cleanup();
  }
}

export async function* stepStream<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(
  loop: LoopHandle<TContext, TObservation, TOutput>,
  context: TContext,
  options: Omit<StepOptions, "traceSink"> = {},
): AsyncGenerator<TraceEvent, Result<StepResult<TContext, TObservation, TOutput>>, void> {
  const events: TraceEvent[] = [];
  const result = await step(loop, context, {
    ...options,
    traceSink: {
      emit: (event) => {
        events.push(event);
        return Promise.resolve(ok(undefined));
      },
    },
  });

  for (const event of events) {
    await Promise.resolve();
    yield event;
  }

  return result;
}

function makeStepRuntime<TContext extends AnyContext>(
  loop: LoopHandle<TContext>,
  context: TContext,
  state: RuntimeState,
  signal: AbortSignal,
  emit: (event: TraceEvent) => Promise<Result<void>>,
): StepRuntime<TContext> {
  return {
    runId: context.runId,
    loopId: loop.id,
    signal,
    registry: state.registry,
    traceSink: { emit },
    now,
    applyPatch: (current) => ok(current),
    callTool: async (toolId, input, options) => {
      const handler = state.registry.tools.get(toolId);
      if (!handler.ok) {
        return handler;
      }
      return handler.value.invoke(input, {
        ...options,
        signal: options?.signal ?? signal,
      });
    },
    runLoop: async (loopId, childContext, options?: RunOptions) => {
      const child = state.registry.loops.get(loopId);
      if (!child.ok) {
        return child;
      }
      const runtime = await import("./run.js");
      return runtime.run(child.value, childContext, {
        ...options,
        signal: options?.signal ?? signal,
      });
    },
  };
}

function makeEmitter(
  primary: TraceSink,
  secondary?: TraceSink,
): (event: TraceEvent) => Promise<Result<void>> {
  return async (event) => {
    const primaryResult = await primary.emit(event);
    if (!primaryResult.ok) {
      return primaryResult;
    }
    if (secondary === undefined || secondary === primary) {
      return ok(undefined);
    }
    return secondary.emit(event);
  };
}

async function persistCompleted(
  emit: (event: TraceEvent) => Promise<Result<void>>,
  trace: Trace,
): Promise<Result<void>> {
  return emit({
    type: "step.completed",
    trace,
    at: now(),
  });
}

function makeTerminalTrace(
  loop: { readonly id: LoopId; readonly version: LoopVersion },
  context: AnyContext,
  traceId: ReturnType<typeof newTraceId>,
  startedAt: ISODateTime,
  startedAtMs: number,
  outcome: "fail" | "cancelled" | "timeout",
  error: LoomError,
): Trace {
  const endedAtMs = Date.now();
  return {
    id: traceId,
    runId: context.runId,
    loopId: loop.id,
    loopVersion: loop.version,
    stepNumber: asStepNumber(context.state.observations.length),
    rootTraceId: traceId,
    startedAt,
    endedAt: now(),
    durationMs: Math.max(0, endedAtMs - startedAtMs) as DurationMs,
    inputContextId: context.id,
    outputContextId: context.id,
    outcome,
    error,
    observations: [],
    decisions: [],
    actions: [],
    children: [],
    tags: [],
  };
}

function abortError(timeout: boolean, traceId: ReturnType<typeof newTraceId>): LoomError {
  return makeLoomError({
    code: timeout ? "TIMEOUT" : "ABORTED",
    message: timeout ? "Operation timed out" : "Operation aborted",
    retryable: timeout,
    traceId,
  });
}

function withTraceId(error: LoomError, traceId: ReturnType<typeof newTraceId>): LoomError {
  return makeLoomError({
    code: error.code,
    message: error.message,
    retryable: error.retryable,
    traceId: error.traceId ?? traceId,
    ...(error.cause === undefined ? {} : { cause: error.cause }),
    ...(error.metadata === undefined ? {} : { metadata: error.metadata }),
  });
}

function makeFallbackState(): RuntimeState {
  return {
    traceStore: {
      append: () => Promise.resolve(ok(undefined)),
      appendEvent: () => Promise.resolve(ok(undefined)),
      get: () =>
        Promise.resolve(
          err(
            makeLoomError({
              code: "VALIDATION_FAILED",
              message: "Trace not found",
              retryable: false,
            }),
          ),
        ),
      query: async function* emptyQuery() {
        await Promise.resolve();
        for (const trace of [] as Trace[]) {
          yield trace;
        }
      },
      children: async function* emptyChildren() {
        await Promise.resolve();
        for (const trace of [] as Trace[]) {
          yield trace;
        }
      },
    },
    traceSink: {
      emit: () => Promise.resolve(ok(undefined)),
    },
    registry: defaultRuntimeRegistry,
  };
}

function now(): ISODateTime {
  return new Date().toISOString() as ISODateTime;
}
