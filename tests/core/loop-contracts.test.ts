import { describe, expect, it } from "vitest";

import {
  asStepNumber,
  emptyAffordances,
  emptyKnowledge,
  emptyState,
  freezeContext,
  newContextId,
  newLoopId,
  newLoopVersion,
  newRunId,
  newTraceId,
  ok,
  type Context,
  type DoneFunction,
  type DurationMs,
  type ISODateTime,
  type LoopHandle,
  type MinimalLoopDefinition,
  type RunOptions,
  type RuntimeRegistry,
  type RuntimeTrace,
  type StepFunction,
  type StepResult,
  type Trace,
  type TraceReader,
} from "../../src/index.js";

const now = "2026-06-04T00:00:00.000Z" as ISODateTime;
const zeroDuration = 0 as DurationMs;

function makeContext(): Context {
  return freezeContext({
    id: newContextId(),
    runId: newRunId(),
    createdAt: now,
    identity: {
      role: "loop tester",
      capabilities: [],
      constraints: [],
    },
    goal: {
      objective: "Exercise loop contracts",
      criteria: [],
      budget: {},
    },
    state: emptyState(),
    knowledge: emptyKnowledge(),
    affordances: emptyAffordances(),
  });
}

const emptyRuntimeTraces: readonly RuntimeTrace[] = [];

function iterateEmptyTraces(): AsyncIterable<RuntimeTrace> {
  return {
    async *[Symbol.asyncIterator]() {
      for (const trace of emptyRuntimeTraces) {
        await Promise.resolve();
        yield trace;
      }
    },
  };
}

function makeTraceReader(): TraceReader {
  return {
    query: () => iterateEmptyTraces(),
    get: () => Promise.resolve(ok(undefined)),
  };
}

function makeContractTrace(context: Context, loopId: ReturnType<typeof newLoopId>): Trace {
  const id = newTraceId();
  return {
    id,
    runId: context.runId,
    loopId,
    loopVersion: newLoopVersion(),
    stepNumber: asStepNumber(0),
    rootTraceId: id,
    startedAt: now,
    endedAt: now,
    durationMs: zeroDuration,
    inputContextId: context.id,
    outputContextId: context.id,
    outcome: "pass",
    observations: [],
    decisions: [],
    actions: [],
    children: [],
    tags: [],
  };
}

function makeRegistry(implementation: StepFunction | DoneFunction): RuntimeRegistry {
  return {
    tools: {
      get: () =>
        ok({
          ref: {
            id: "tool",
            description: "tool",
          },
          invoke: () =>
            Promise.resolve(
              ok({
                id: "obs",
                source: "tool",
                value: null,
                at: now,
              }),
            ),
        }),
    },
    loops: {
      get: () =>
        ok({
          id: newLoopId(),
          version: newLoopVersion(),
          definition: {} as MinimalLoopDefinition,
          traceReader: makeTraceReader(),
          createdAt: now,
        }),
    },
    evaluators: {
      get: () =>
        ok({
          evaluate: () => Promise.resolve(ok(true)),
        }),
    },
    implementations: {
      get: () => ok(implementation),
    },
  };
}

describe("MinimalLoop contracts", () => {
  it("allows a step function to return Promise<Result<StepResult>>", async () => {
    const context = makeContext();
    const step: StepFunction = (
      input,
      runtime,
    ) =>
      Promise.resolve(
        ok({
          context: input,
          trace: makeContractTrace(input, runtime.loopId),
        } satisfies StepResult),
      );

    const result = await step(context, {
      runId: context.runId,
      loopId: newLoopId(),
      signal: new AbortController().signal,
      registry: makeRegistry(step),
      traceSink: {
        emit: () => Promise.resolve(ok(undefined)),
      },
      now: () => now,
      applyPatch: (current) => ok(current),
      callTool: () =>
        Promise.resolve(
          ok({
            id: "obs",
            source: "tool",
            value: null,
            at: now,
          }),
        ),
      runLoop: () =>
        Promise.resolve(
          ok({
            context,
            traces: [],
            metrics: {
              steps: 0,
              startedAt: now,
              endedAt: now,
              durationMs: zeroDuration,
              traceCount: 0,
              outcome: "pass",
            },
          }),
        ),
    });

    expect(result.ok).toBe(true);
  });

  it("allows done functions to return MaybePromise<Result<boolean>>", async () => {
    const done: DoneFunction = () => ok(true);

    const result = await done(makeContext(), {
      signal: new AbortController().signal,
      now: () => now,
      registry: makeRegistry(done),
    });

    expect(result).toEqual(ok(true));
  });

  it("models immutable versioned definitions and handles with trace readers", () => {
    const definition: MinimalLoopDefinition = {
      id: newLoopId(),
      version: newLoopVersion(),
      identity: {
        role: "contract loop",
        capabilities: [],
        constraints: [],
      },
      goal: {
        objective: "Define loop",
        criteria: [],
        budget: {},
      },
      step: (context) =>
        Promise.resolve(
          ok({
            context,
            trace: makeContractTrace(context, definition.id),
          }),
        ),
      done: () => ok(true),
    };
    const handle: LoopHandle = {
      id: definition.id,
      version: definition.version,
      definition,
      traceReader: makeTraceReader(),
      createdAt: now,
    };

    expect(handle.definition.identity.role).toBe("contract loop");
    expect(handle.createdAt).toBe(now);
  });

  it("exposes run options for cancellation, timeout, and max steps", () => {
    const signal = new AbortController().signal;
    const options: RunOptions = {
      signal,
      timeoutMs: 100,
      maxSteps: 3,
      metadata: { test: true },
    };

    expect(options.signal).toBe(signal);
    expect(options.timeoutMs).toBe(100);
    expect(options.maxSteps).toBe(3);
    expect(asStepNumber(0)).toBe(0);
  });
});
