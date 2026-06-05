import { describe, expect, it } from "vitest";

import {
  buildMessages,
  buildSystemPrompt,
  buildUserPrompt,
  emptyKnowledge,
  emptyState,
  freezeContext,
  newContextId,
  newRunId,
  type Action,
  type Context,
  type Decision,
  type ISODateTime,
  type KnowledgeItem,
  type Observation,
} from "../../src/index.js";

const now = "2026-06-04T00:00:00.000Z" as ISODateTime;

function makeContext(): Context {
  const observation: Observation = {
    id: "obs-1",
    source: "sensor",
    value: { status: "ready" },
    at: now,
  };
  const action: Action = {
    id: "action-1",
    kind: "tool",
    description: "Search the index",
    target: "search",
    input: { query: "loom" },
  };
  const decision: Decision = {
    id: "decision-1",
    action,
    reasoning: "Need external context",
    alternatives: [],
    confidence: 0.75,
    at: now,
  };
  const fact: KnowledgeItem = {
    id: "fact-1",
    kind: "fact",
    content: "The index contains project notes.",
    confidence: 0.9,
    createdAt: now,
  };
  const heuristic: KnowledgeItem = {
    id: "heuristic-1",
    kind: "heuristic",
    content: "Prefer reversible steps.",
    confidence: 0.8,
    createdAt: now,
  };

  return freezeContext({
    id: newContextId(),
    runId: newRunId(),
    createdAt: now,
    identity: {
      role: "research planner",
      capabilities: [
        {
          id: "plan",
          description: "Create next-step plans",
        },
      ],
      constraints: [
        {
          id: "json-only",
          description: "Return machine-readable decisions",
          severity: "must",
        },
        {
          id: "concise",
          description: "Keep reasoning concise",
          severity: "should",
        },
      ],
    },
    goal: {
      objective: "Find the next useful action",
      criteria: [
        {
          id: "actionable",
          description: "The selected action can be executed",
          required: true,
        },
      ],
      budget: {
        maxSteps: 4,
        maxTokens: 1000,
      },
    },
    state: {
      ...emptyState(),
      observations: [observation],
      decisions: [decision],
    },
    knowledge: {
      ...emptyKnowledge(),
      facts: [fact],
      heuristics: [heuristic],
    },
    affordances: {
      tools: [
        {
          id: "search",
          description: "Search indexed notes",
          inputSchema: {
            type: "object",
            properties: {
              query: { type: "string" },
            },
            required: ["query"],
          },
        },
      ],
      loops: [],
      resources: [],
    },
  });
}

describe("prompt builder", () => {
  it("builds a system prompt from identity, goal, constraints, and tools", () => {
    const prompt = buildSystemPrompt(makeContext());

    expect(prompt).toContain("research planner");
    expect(prompt).toContain("Create next-step plans");
    expect(prompt).toContain("MUST: Return machine-readable decisions");
    expect(prompt).toContain("Find the next useful action");
    expect(prompt).toContain("The selected action can be executed");
    expect(prompt).toContain("search");
    expect(prompt).toContain("decision");
    expect(prompt).toContain("confidence");
  });

  it("builds a user prompt with recent state, knowledge, and budget", () => {
    const prompt = buildUserPrompt(makeContext(), { maxHistorySteps: 1 });

    expect(prompt).toContain("Step number: 1");
    expect(prompt).toContain('"status": "ready"');
    expect(prompt).toContain("Need external context");
    expect(prompt).toContain("The index contains project notes.");
    expect(prompt).toContain("Prefer reversible steps.");
    expect(prompt).toContain("maxTokens: 1000");
  });

  it("can omit knowledge and history from the user prompt", () => {
    const prompt = buildUserPrompt(makeContext(), {
      includeHistory: false,
      includeKnowledge: false,
    });

    expect(prompt).not.toContain("The index contains project notes.");
    expect(prompt).not.toContain("Need external context");
    expect(prompt).toContain("Step number: 1");
  });

  it("returns system and user messages in order", () => {
    const messages = buildMessages(makeContext());

    expect(messages).toHaveLength(2);
    expect(messages[0]?.role).toBe("system");
    expect(messages[1]?.role).toBe("user");
  });
});
