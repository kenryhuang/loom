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
  type Action,
  type Context,
  type Decision,
  type DurationMs,
  type ISODateTime,
  type MinimalLoopDefinition,
  type Observation,
  type Trace,
} from "../index.js";

const zeroDuration = 0 as DurationMs;

export function makeInitialCounterContext(maxSteps = 1): Context {
  return freezeContext({
    id: newContextId(),
    runId: newRunId(),
    createdAt: now(),
    identity: {
      role: "minimal counter",
      capabilities: [
        {
          id: "count",
          description: "Append counter observations",
        },
      ],
      constraints: [
        {
          id: "json-compatible",
          description: "Only store JSON-compatible values",
          severity: "must",
        },
      ],
    },
    goal: {
      objective: "Count until the step budget is reached",
      criteria: [
        {
          id: "budget-reached",
          description: "Observation count reaches maxSteps",
          required: true,
        },
      ],
      budget: { maxSteps },
    },
    state: emptyState(),
    knowledge: emptyKnowledge(),
    affordances: emptyAffordances(),
  });
}

export function makeMinimalCounterLoop(): MinimalLoopDefinition {
  const id = newLoopId();
  const version = newLoopVersion();

  return {
    id,
    version,
    identity: {
      role: "minimal counter loop",
      capabilities: [
        {
          id: "append-counter",
          description: "Append the next counter observation",
        },
      ],
      constraints: [],
    },
    goal: {
      objective: "Append counter observations",
      criteria: [],
      budget: {},
    },
    step: (context) => {
      const counter = context.state.observations.length + 1;
      const at = now();
      const action: Action = {
        id: `record-counter-${String(counter)}`,
        kind: "custom",
        description: "Record counter observation",
        input: { counter },
      };
      const observation: Observation = {
        id: `counter-${String(counter)}`,
        source: "minimal-counter",
        value: { counter },
        at,
      };
      const decision: Decision = {
        id: `decision-${String(counter)}`,
        action,
        reasoning: `Counter advanced to ${String(counter)}`,
        alternatives: [
          {
            id: "no-op",
            kind: "none",
            description: "Stop without recording",
          },
        ],
        confidence: 1,
        at,
      };
      const next = freezeContext({
        ...context,
        id: newContextId(),
        state: {
          ...context.state,
          observations: [...context.state.observations, observation],
          decisions: [...context.state.decisions, decision],
        },
      });

      return Promise.resolve(
        ok({
          context: next,
          trace: makeStepTrace(context, next, id, version, observation, decision, action, at),
        }),
      );
    },
    done: (context) => {
      const maxSteps = context.goal.budget.maxSteps;
      return ok(maxSteps !== undefined && context.state.observations.length >= maxSteps);
    },
  };
}

function makeStepTrace(
  input: Context,
  output: Context,
  loopId: ReturnType<typeof newLoopId>,
  loopVersion: ReturnType<typeof newLoopVersion>,
  observation: Observation,
  decision: Decision,
  action: Action,
  at: ISODateTime,
): Trace {
  const id = newTraceId();
  return {
    id,
    runId: input.runId,
    loopId,
    loopVersion,
    stepNumber: asStepNumber(input.state.observations.length),
    rootTraceId: id,
    startedAt: at,
    endedAt: now(),
    durationMs: zeroDuration,
    inputContextId: input.id,
    outputContextId: output.id,
    outcome: "pass",
    observations: [observation],
    decisions: [decision],
    actions: [action],
    children: [],
    tags: ["example", "minimal-counter"],
  };
}

function now(): ISODateTime {
  return new Date().toISOString() as ISODateTime;
}
