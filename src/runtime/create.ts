import { deepFreeze } from "../core/immutable.js";
import { makeLoomError } from "../core/errors.js";
import type {
  CreateOptions,
  LoopHandle,
  MinimalLoopDefinition,
  TraceOptions,
  TraceReader,
} from "../core/loop.js";
import type { AnyContext, Observation } from "../core/context.js";
import type { ISODateTime } from "../core/ids.js";
import type { JsonValue } from "../core/json.js";
import { err, ok, type Result } from "../core/result.js";
import {
  createInMemoryTraceSink,
  InMemoryTraceStore,
} from "../observability/in-memory-trace-store.js";
import type { TraceQuery, TraceStore } from "../observability/trace-store.js";
import {
  defaultRuntimeRegistry,
  setRuntimeState,
  type RuntimeState,
} from "./registry.js";

export function create<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(
  definition: MinimalLoopDefinition<TContext, TObservation, TOutput>,
  options: CreateOptions = {},
): Result<LoopHandle<TContext, TObservation, TOutput>> {
  const validation = validateDefinition(definition);
  if (!validation.ok) {
    return validation;
  }

  const traceStore = resolveTraceStore(options.traceStore);
  if (!traceStore.ok) {
    return traceStore;
  }

  const state: RuntimeState = {
    traceStore: traceStore.value,
    traceSink: createInMemoryTraceSink(traceStore.value),
    registry: options.registry ?? defaultRuntimeRegistry,
  };
  const frozenDefinition = deepFreeze(definition) as MinimalLoopDefinition<
    TContext,
    TObservation,
    TOutput
  >;
  const handle: LoopHandle<TContext, TObservation, TOutput> = {
    id: frozenDefinition.id,
    version: frozenDefinition.version,
    definition: frozenDefinition,
    traceReader: makeTraceReader(traceStore.value),
    createdAt: now(),
  };

  setRuntimeState(handle, state);
  return ok(handle);
}

function validateDefinition<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(definition: MinimalLoopDefinition<TContext, TObservation, TOutput>): Result<void> {
  if (definition.id.length === 0) {
    return validationFailed("Loop definition id is required");
  }
  if (definition.version.length === 0) {
    return validationFailed("Loop definition version is required");
  }
  if (definition.identity.role.length === 0) {
    return validationFailed("Loop definition identity.role is required");
  }
  if (definition.goal.objective.length === 0) {
    return validationFailed("Loop definition goal.objective is required");
  }
  if (typeof definition.step !== "function") {
    return validationFailed("Loop definition step must be a function");
  }
  if (typeof definition.done !== "function") {
    return validationFailed("Loop definition done must be a function");
  }
  return ok(undefined);
}

function resolveTraceStore(traceStore: unknown): Result<TraceStore> {
  if (traceStore === undefined) {
    return ok(new InMemoryTraceStore());
  }

  if (isTraceStore(traceStore)) {
    return ok(traceStore);
  }

  return validationFailed("traceStore must implement TraceStore");
}

function isTraceStore(value: unknown): value is TraceStore {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Partial<TraceStore>;
  return (
    typeof candidate.append === "function" &&
    typeof candidate.appendEvent === "function" &&
    typeof candidate.get === "function" &&
    typeof candidate.query === "function" &&
    typeof candidate.children === "function"
  );
}

function makeTraceReader(traceStore: TraceStore): TraceReader {
  return {
    query: (options) => traceStore.query(toTraceQuery(options)),
    get: (id) => traceStore.get(id),
  };
}

function toTraceQuery(options: TraceOptions = {}): TraceQuery {
  return {
    ...(options.runId === undefined ? {} : { runId: options.runId }),
    ...(options.loopId === undefined ? {} : { loopId: options.loopId }),
    ...(options.rootTraceId === undefined ? {} : { rootTraceId: options.rootTraceId }),
    ...(options.parentTraceId === undefined
      ? {}
      : { parentTraceId: options.parentTraceId }),
    ...(options.outcome === undefined ? {} : { outcome: options.outcome }),
    ...(options.tags === undefined ? {} : { tags: options.tags }),
    ...(options.limit === undefined ? {} : { limit: options.limit }),
  };
}

function validationFailed<T>(message: string): Result<T> {
  return err(
    makeLoomError({
      code: "VALIDATION_FAILED",
      message,
      retryable: false,
    }),
  );
}

function now(): ISODateTime {
  return new Date().toISOString() as ISODateTime;
}
