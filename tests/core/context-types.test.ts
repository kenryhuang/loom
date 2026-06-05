import { describe, expect, it } from "vitest";

import {
  deepFreeze,
  emptyAffordances,
  emptyKnowledge,
  emptyState,
  freezeContext,
  newContextId,
  newRunId,
  type Context,
  type ISODateTime,
  type KnowledgeItem,
  type Observation,
} from "../../src/index.js";

const now = "2026-06-04T00:00:00.000Z" as ISODateTime;

function makeContext(): Context {
  const fact: KnowledgeItem = {
    id: "fact-1",
    kind: "fact",
    content: "known",
    confidence: 0.9,
    createdAt: now,
  };

  return {
    id: newContextId(),
    runId: newRunId(),
    createdAt: now,
    identity: {
      role: "counter",
      capabilities: [
        {
          id: "count",
          description: "Increment a counter",
        },
      ],
      constraints: [
        {
          id: "json-only",
          description: "Only emit JSON-compatible values",
          severity: "must",
        },
      ],
    },
    goal: {
      objective: "Count once",
      criteria: [
        {
          id: "observed",
          description: "At least one observation exists",
          required: true,
        },
      ],
      budget: {
        maxSteps: 1,
      },
    },
    state: {
      ...emptyState(),
      observations: [
        {
          id: "obs-1",
          source: "test",
          value: { count: 0 },
          at: now,
        },
      ],
    },
    knowledge: {
      ...emptyKnowledge(),
      facts: [fact],
    },
    affordances: emptyAffordances(),
  };
}

describe("Context five-layer contract", () => {
  it("requires identity, goal, state, knowledge, and affordances", () => {
    const context = makeContext();

    expect(context.identity.role).toBe("counter");
    expect(context.goal.objective).toBe("Count once");
    expect(context.state.observations).toHaveLength(1);
    expect(context.knowledge.facts[0]?.kind).toBe("fact");
    expect(context.affordances.tools).toEqual([]);
  });

  it("creates empty append-only state and empty knowledge/affordance layers", () => {
    expect(emptyState()).toEqual({
      observations: [],
      decisions: [],
      pending: [],
    });
    expect(emptyKnowledge()).toEqual({
      facts: [],
      heuristics: [],
      memories: [],
      version: "v1",
    });
    expect(emptyAffordances()).toEqual({
      tools: [],
      loops: [],
      resources: [],
    });
  });

  it("recursively freezes plain objects and arrays", () => {
    const value = {
      nested: {
        list: [{ value: 1 }],
      },
    };
    const frozen = deepFreeze(value);

    expect(Object.isFrozen(frozen)).toBe(true);
    expect(Object.isFrozen(frozen.nested)).toBe(true);
    expect(Object.isFrozen(frozen.nested.list)).toBe(true);
    expect(Object.isFrozen(frozen.nested.list[0])).toBe(true);
  });

  it("freezes every context layer while preserving nested object identity", () => {
    const context = makeContext();
    const identity = context.identity;
    const goal = context.goal;
    const state = context.state;
    const knowledge = context.knowledge;
    const affordances = context.affordances;

    const frozen = freezeContext(context);

    expect(frozen.identity).toBe(identity);
    expect(frozen.goal).toBe(goal);
    expect(frozen.state).toBe(state);
    expect(frozen.knowledge).toBe(knowledge);
    expect(frozen.affordances).toBe(affordances);
    expect(Object.isFrozen(frozen.identity.capabilities)).toBe(true);
    expect(Object.isFrozen(frozen.goal.criteria)).toBe(true);
    expect(Object.isFrozen(frozen.state.observations)).toBe(true);
    expect(Object.isFrozen(frozen.knowledge.facts)).toBe(true);
    expect(Object.isFrozen(frozen.affordances.tools)).toBe(true);
  });

  it("keeps state arrays append-only by requiring new arrays for new state", () => {
    const context = freezeContext(makeContext());
    const observation: Observation = {
      id: "obs-2",
      source: "test",
      value: { count: 1 },
      at: now,
    };
    const nextState = {
      ...context.state,
      observations: [...context.state.observations, observation],
    };

    expect(nextState.observations).toHaveLength(2);
    expect(context.state.observations).toHaveLength(1);
  });
});
