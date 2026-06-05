import { describe, expect, it } from "vitest";

import {
  asStepNumber,
  create,
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
  step,
  type Context,
  type DurationMs,
  type ISODateTime,
  type LoopId,
  type LoopVersion,
  type MinimalLoopDefinition,
  type Result,
  type Trace,
} from "../../src/index.js";

const now = "2026-06-04T00:00:00.000Z" as ISODateTime;
const oneMs = 1 as DurationMs;

function unwrap<T>(result: Result<T>): T {
  if (!result.ok) {
    throw new Error(result.error.message);
  }
  return result.value;
}

function makeContext(): Context {
  return freezeContext({
    id: newContextId(),
    runId: newRunId(),
    createdAt: now,
    identity: {
      role: "error test",
      capabilities: [],
      constraints: [],
    },
    goal: {
      objective: "Exercise errors",
      criteria: [],
      budget: { maxSteps: 1 },
    },
    state: emptyState(),
    knowledge: emptyKnowledge(),
    affordances: emptyAffordances(),
  });
}

async function collect(query: AsyncIterable<Trace>): Promise<readonly Trace[]> {
  const traces: Trace[] = [];
  for await (const trace of query) {
    traces.push(trace);
  }
  return traces;
}

function makeTrace(context: Context, loopId: LoopId, loopVersion: LoopVersion): Trace {
  const id = newTraceId();
  return {
    id,
    runId: context.runId,
    loopId,
    loopVersion,
    stepNumber: asStepNumber(0),
    rootTraceId: id,
    startedAt: now,
    endedAt: now,
    durationMs: oneMs,
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

describe("runtime errors and cancellation", () => {
  it("converts thrown loop-author errors to INTERNAL and persists fail traces", async () => {
    const definition: MinimalLoopDefinition = {
      id: newLoopId(),
      version: newLoopVersion(),
      identity: { role: "thrower", capabilities: [], constraints: [] },
      goal: { objective: "Throw", criteria: [], budget: { maxSteps: 1 } },
      step: () => {
        throw new Error("boom");
      },
      done: () => ok(false),
    };
    const handle = unwrap(create(definition));
    const result = await step(handle, makeContext());

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("INTERNAL");
      expect(result.error.retryable).toBe(false);
    }

    const traces = await collect(handle.traceReader.query({ outcome: ["fail"] }));
    expect(traces).toHaveLength(1);
    expect(traces[0]?.error?.code).toBe("INTERNAL");
  });

  it("returns ABORTED without calling loop step and persists cancelled traces", async () => {
    let calls = 0;
    const definition: MinimalLoopDefinition = {
      id: newLoopId(),
      version: newLoopVersion(),
      identity: { role: "abortable", capabilities: [], constraints: [] },
      goal: { objective: "Abort", criteria: [], budget: { maxSteps: 1 } },
      step: (context) => {
        calls += 1;
        return Promise.resolve(
          ok({
            context,
            trace: makeTrace(context, definition.id, definition.version),
          }),
        );
      },
      done: () => ok(false),
    };
    const handle = unwrap(create(definition));
    const controller = new AbortController();
    controller.abort();

    const result = await step(handle, makeContext(), { signal: controller.signal });

    expect(calls).toBe(0);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("ABORTED");
    }

    const traces = await collect(handle.traceReader.query({ outcome: ["cancelled"] }));
    expect(traces).toHaveLength(1);
    expect(traces[0]?.error?.code).toBe("ABORTED");
  });
});
