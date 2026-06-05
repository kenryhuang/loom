import { describe, expect, it } from "vitest";

import {
  asStepNumber,
  create,
  createRuntimeRegistry,
  done,
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
  run,
  step,
  type Context,
  type CriterionEvaluator,
  type DurationMs,
  type EvaluatorRef,
  type ISODateTime,
  type LoopId,
  type LoopVersion,
  type MinimalLoopDefinition,
  type Observation,
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

function makeContext(maxSteps = 1): Context {
  return freezeContext({
    id: newContextId(),
    runId: newRunId(),
    createdAt: now,
    identity: {
      role: "runtime test",
      capabilities: [],
      constraints: [],
    },
    goal: {
      objective: "Run a minimal loop",
      criteria: [],
      budget: { maxSteps },
    },
    state: emptyState(),
    knowledge: emptyKnowledge(),
    affordances: emptyAffordances(),
  });
}

function makeTrace(
  input: Context,
  output: Context,
  loopId: LoopId,
  loopVersion: LoopVersion,
): Trace {
  const id = newTraceId();
  return {
    id,
    runId: input.runId,
    loopId,
    loopVersion,
    stepNumber: asStepNumber(input.state.observations.length),
    rootTraceId: id,
    startedAt: now,
    endedAt: now,
    durationMs: oneMs,
    inputContextId: input.id,
    outputContextId: output.id,
    outcome: "pass",
    observations: [...output.state.observations],
    decisions: [...output.state.decisions],
    actions: [],
    children: [],
    tags: ["runtime"],
  };
}

function makeOneStepDefinition(): MinimalLoopDefinition {
  const id = newLoopId();
  const version = newLoopVersion();
  return {
    id,
    version,
    identity: {
      role: "one-step loop",
      capabilities: [],
      constraints: [],
    },
    goal: {
      objective: "Append one observation",
      criteria: [],
      budget: { maxSteps: 1 },
    },
    step: (context) => {
      const observation: Observation = {
        id: `obs-${String(context.state.observations.length + 1)}`,
        source: "runtime-test",
        value: { count: context.state.observations.length + 1 },
        at: now,
      };
      const next = freezeContext({
        ...context,
        id: newContextId(),
        state: {
          ...context.state,
          observations: [...context.state.observations, observation],
        },
      });

      return Promise.resolve(
        ok({
          context: next,
          trace: makeTrace(context, next, id, version),
        }),
      );
    },
    done: (context) => ok(context.state.observations.length >= 1),
  };
}

describe("minimal loop runtime", () => {
  it("creates, steps, checks done, and runs a one-step loop", async () => {
    const context = makeContext(1);
    const handle = unwrap(create(makeOneStepDefinition()));

    const stepResult = unwrap(await step(handle, context));

    expect(stepResult.context.state.observations).toHaveLength(1);
    expect(Object.isFrozen(stepResult.context)).toBe(true);
    expect(Object.isFrozen(stepResult.context.state.observations)).toBe(true);
    expect(stepResult.trace.outcome).toBe("pass");

    await expect(done(handle, stepResult.context)).resolves.toEqual(ok(true));

    const runHandle = unwrap(create(makeOneStepDefinition()));
    const runResult = unwrap(await run(runHandle, context));

    expect(runResult.context.state.observations).toHaveLength(1);
    expect(runResult.traces).toHaveLength(1);
    expect(runResult.metrics.steps).toBe(1);
    expect(runResult.metrics.outcome).toBe("pass");
  });

  it("persists pass traces through the handle trace reader", async () => {
    const context = makeContext(1);
    const handle = unwrap(create(makeOneStepDefinition()));
    const stepResult = unwrap(await step(handle, context));
    const stored = await handle.traceReader.get(stepResult.trace.id);

    expect(stored).toEqual(ok(stepResult.trace));
  });

  it("returns done when a required success criterion evaluator passes", async () => {
    const evaluatorRef = "always-pass" as EvaluatorRef;
    const evaluator: CriterionEvaluator = {
      evaluate: () => Promise.resolve(ok(true)),
    };
    const context = freezeContext({
      ...makeContext(10),
      goal: {
        objective: "Use evaluator",
        criteria: [
          {
            id: "criterion",
            description: "Always passes",
            evaluator: evaluatorRef,
            required: true,
          },
        ],
        budget: {},
      },
    });
    const handle = unwrap(
      create(makeOneStepDefinition(), {
        registry: createRuntimeRegistry({
          evaluators: new Map([[evaluatorRef, evaluator]]),
        }),
      }),
    );

    await expect(done(handle, context)).resolves.toEqual(ok(true));
  });

  it("returns done when the goal step budget is exhausted", async () => {
    const handle = unwrap(create(makeOneStepDefinition()));

    await expect(done(handle, makeContext(0))).resolves.toEqual(ok(true));
  });
});
