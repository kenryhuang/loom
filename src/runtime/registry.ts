import type {
  CriterionEvaluator,
  DoneFunction,
  ImplementationRegistry,
  LoopHandle,
  LoopRegistryView,
  RuntimeRegistry,
  StepFunction,
  ToolHandler,
  ToolRegistry,
} from "../core/loop.js";
import type { EvaluatorRef, ImplementationRef } from "../core/context.js";
import type { LoopId, LoopVersion } from "../core/ids.js";
import { makeLoomError } from "../core/errors.js";
import { err, ok, type Result } from "../core/result.js";
import type { TraceSink } from "../core/trace.js";
import type { TraceStore } from "../observability/trace-store.js";

export interface RuntimeRegistryOptions {
  readonly tools?: ReadonlyMap<string, ToolHandler>;
  readonly loops?: ReadonlyMap<string, LoopHandle>;
  readonly evaluators?: ReadonlyMap<EvaluatorRef, CriterionEvaluator>;
  readonly implementations?: ReadonlyMap<ImplementationRef, StepFunction | DoneFunction>;
}

export interface RuntimeState {
  readonly traceStore: TraceStore;
  readonly traceSink: TraceSink;
  readonly registry: RuntimeRegistry;
}

const stateByHandle = new WeakMap<object, RuntimeState>();

export const defaultRuntimeRegistry: RuntimeRegistry = createRuntimeRegistry();

export function createRuntimeRegistry(options: RuntimeRegistryOptions = {}): RuntimeRegistry {
  const tools = options.tools ?? new Map<string, ToolHandler>();
  const loops = options.loops ?? new Map<string, LoopHandle>();
  const evaluators = options.evaluators ?? new Map<EvaluatorRef, CriterionEvaluator>();
  const implementations =
    options.implementations ?? new Map<ImplementationRef, StepFunction | DoneFunction>();

  return {
    tools: makeToolRegistry(tools),
    loops: makeLoopRegistry(loops),
    evaluators: makeEvaluatorRegistry(evaluators),
    implementations: makeImplementationRegistry(implementations),
  };
}

export function setRuntimeState(handle: object, state: RuntimeState): void {
  stateByHandle.set(handle, state);
}

export function getRuntimeState(handle: object): RuntimeState | undefined {
  return stateByHandle.get(handle);
}

function makeToolRegistry(tools: ReadonlyMap<string, ToolHandler>): ToolRegistry {
  return {
    get: (toolId) => {
      const handler = tools.get(toolId);
      return handler === undefined ? missing("Tool not found") : ok(handler);
    },
  };
}

function makeLoopRegistry(loops: ReadonlyMap<string, LoopHandle>): LoopRegistryView {
  return {
    get: (loopId, version) => {
      const exact = loops.get(loopKey(loopId, version));
      const fallback = loops.get(loopId);
      const handle = exact ?? fallback;
      return handle === undefined ? missing("Loop not found") : ok(handle);
    },
  };
}

function makeEvaluatorRegistry(
  evaluators: ReadonlyMap<EvaluatorRef, CriterionEvaluator>,
): RuntimeRegistry["evaluators"] {
  return {
    get: (ref) => {
      const evaluator = evaluators.get(ref);
      return evaluator === undefined ? missing("Evaluator not found") : ok(evaluator);
    },
  };
}

function makeImplementationRegistry(
  implementations: ReadonlyMap<ImplementationRef, StepFunction | DoneFunction>,
): ImplementationRegistry {
  return {
    get: (ref) => {
      const implementation = implementations.get(ref);
      return implementation === undefined
        ? missing("Implementation not found")
        : ok(implementation);
    },
  };
}

function loopKey(loopId: LoopId, version?: LoopVersion): string {
  return version === undefined ? loopId : `${loopId}@${version}`;
}

function missing<T>(message: string): Result<T> {
  return err(
    makeLoomError({
      code: "VALIDATION_FAILED",
      message,
      retryable: false,
    }),
  );
}
