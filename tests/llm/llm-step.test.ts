import { describe, expect, it, vi } from "vitest";

import {
  createLlmStepFunction,
  emptyKnowledge,
  emptyState,
  freezeContext,
  makeLoomError,
  newContextId,
  newLoopId,
  newRunId,
  ok,
  type AnyContext,
  type Context,
  type ISODateTime,
  type JsonValue,
  type LlmProvider,
  type LlmResponse,
  type Observation,
  type Result,
  type RuntimeRegistry,
  type StepRuntime,
} from "../../src/index.js";

const now = "2026-06-04T00:00:00.000Z" as ISODateTime;

function makeContext(maxTokens?: number): Context {
  return freezeContext({
    id: newContextId(),
    runId: newRunId(),
    createdAt: now,
    identity: {
      role: "LLM planner",
      capabilities: [
        {
          id: "decide",
          description: "Decide the next action",
        },
      ],
      constraints: [],
    },
    goal: {
      objective: "Choose the next action",
      criteria: [],
      budget: maxTokens === undefined ? {} : { maxTokens },
    },
    state: emptyState(),
    knowledge: emptyKnowledge(),
    affordances: {
      tools: [
        {
          id: "search",
          description: "Search notes",
          inputSchema: {
            type: "object",
            properties: {
              query: { type: "string" },
            },
          },
        },
      ],
      loops: [],
      resources: [],
    },
  });
}

function makeResponse(input: Partial<LlmResponse>): LlmResponse {
  return {
    content: "{}",
    toolCalls: [],
    usage: { promptTokens: 0, completionTokens: 0, totalTokens: 0 },
    finishReason: "stop",
    ...input,
  };
}

function makeProvider(results: readonly Result<LlmResponse>[]): LlmProvider {
  let index = 0;
  return {
    model: "mock-model",
    chat: vi.fn(() => {
      const result = results[index];
      index += 1;
      if (result === undefined) {
        return Promise.resolve(
          ok(
            makeResponse({
              content:
                "{\"reasoning\":\"default\",\"action\":{\"kind\":\"none\",\"description\":\"Stop\"},\"alternatives\":[],\"confidence\":0.1}",
            }),
          ),
        );
      }
      return Promise.resolve(result);
    }),
  };
}

function makeRuntime(
  callTool: StepRuntime["callTool"] = () =>
    Promise.resolve(
      ok({
        id: "tool-obs",
        source: "search",
        value: { result: "found" },
        at: now,
      }),
    ),
): StepRuntime<AnyContext> {
  const registry: RuntimeRegistry = {
    tools: {
      get: () =>
        ok({
          ref: {
            id: "search",
            description: "Search notes",
          },
          invoke: (input: JsonValue) =>
            Promise.resolve(
              ok({
                id: "tool-obs",
                source: "search",
                value: input,
                at: now,
              }),
            ),
        }),
    },
    loops: {
      get: () =>
        ({
          ok: false,
          error: makeLoomError({
            code: "VALIDATION_FAILED",
            message: "Loop not found",
            retryable: false,
          }),
        }) as never,
    },
    evaluators: {
      get: () =>
        ({
          ok: false,
          error: makeLoomError({
            code: "VALIDATION_FAILED",
            message: "Evaluator not found",
            retryable: false,
          }),
        }) as never,
    },
    implementations: {
      get: () =>
        ({
          ok: false,
          error: makeLoomError({
            code: "VALIDATION_FAILED",
            message: "Implementation not found",
            retryable: false,
          }),
        }) as never,
    },
  };

  return {
    runId: newRunId(),
    loopId: newLoopId(),
    signal: new AbortController().signal,
    registry,
    traceSink: {
      emit: () => Promise.resolve(ok(undefined)),
    },
    now: () => now,
    applyPatch: (context) => ok(context),
    callTool,
    runLoop: () =>
      Promise.resolve(
        ({
          ok: false,
          error: makeLoomError({
            code: "LOOP_FAILED",
            message: "not implemented",
            retryable: false,
          }),
        }) as never,
      ),
  };
}

describe("LLM step function", () => {
  it("turns a structured LLM decision into context state, trace, and output", async () => {
    const provider = makeProvider([
      ok(
        makeResponse({
          content: JSON.stringify({
            reasoning: "No tool needed",
            action: {
              kind: "none",
              description: "Wait for more information",
            },
            alternatives: [
              {
                kind: "custom",
                description: "Ask for clarification",
              },
            ],
            confidence: 0.9,
          }),
          usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15 },
        }),
      ),
    ]);
    const step = createLlmStepFunction({ provider, enableToolCalling: false });

    const result = await step(makeContext(), makeRuntime());

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.context.state.observations).toHaveLength(1);
      expect(result.value.context.state.decisions).toHaveLength(1);
      expect(result.value.context.state.decisions[0]?.reasoning).toBe("No tool needed");
      expect(result.value.context.state.decisions[0]?.confidence).toBe(0.9);
      expect(result.value.trace.actions[0]?.kind).toBe("none");
      expect(result.value.trace.metadata?.model).toBe("mock-model");
      expect(result.value.output).toMatchObject({ reasoning: "No tool needed" });
    }
  });

  it("executes tool calls, appends tool observations to messages, and asks the LLM again", async () => {
    const provider = makeProvider([
      ok(
        makeResponse({
          content: null,
          toolCalls: [
            {
              id: "call_1",
              name: "search",
              arguments: "{\"query\":\"loom\"}",
            },
          ],
          finishReason: "tool_calls",
        }),
      ),
      ok(
        makeResponse({
          content: JSON.stringify({
            reasoning: "Tool result is enough",
            action: {
              kind: "tool",
              target: "search",
              description: "Use the search result",
              input: { query: "loom" },
            },
            alternatives: [],
            confidence: 0.8,
          }),
        }),
      ),
    ]);
    const callTool = vi.fn(() =>
      Promise.resolve(
        ok({
          id: "search-obs",
          source: "search",
          value: { result: "found loom notes" },
          at: now,
        }),
      ),
    );
    const step = createLlmStepFunction({
      provider,
      enableToolCalling: true,
      maxToolCallsPerStep: 2,
    });

    const result = await step(makeContext(), makeRuntime(callTool));

    expect(result.ok).toBe(true);
    expect(callTool).toHaveBeenCalledWith(
      "search",
      { query: "loom" },
      expect.objectContaining({
        metadata: expect.objectContaining({ toolCallId: "call_1" }),
      }),
    );
    expect(provider.chat).toHaveBeenCalledTimes(2);
    const secondCallMessages = (provider.chat as ReturnType<typeof vi.fn>).mock
      .calls[1]?.[0] as readonly { readonly role: string; readonly tool_call_id?: string }[];
    expect(secondCallMessages.some((message) => message.role === "tool")).toBe(true);
    expect(secondCallMessages.some((message) => message.tool_call_id === "call_1")).toBe(true);
    if (result.ok) {
      expect(result.value.context.state.observations).toHaveLength(2);
      expect(result.value.context.state.observations[0]?.source).toBe("search");
      expect(result.value.context.state.decisions[0]?.action.target).toBe("search");
    }
  });

  it("falls back to a custom action when the final response is not JSON", async () => {
    const provider = makeProvider([
      ok(
        makeResponse({
          content: "plain text decision",
        }),
      ),
    ]);
    const step = createLlmStepFunction({ provider, enableToolCalling: false });

    const result = await step(makeContext(), makeRuntime());

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.context.state.decisions[0]?.reasoning).toBe("plain text decision");
      expect(result.value.context.state.decisions[0]?.action.kind).toBe("custom");
    }
  });

  it("returns TOKEN_BUDGET_EXCEEDED when cumulative provider usage exceeds goal budget", async () => {
    const provider = makeProvider([
      ok(
        makeResponse({
          content: JSON.stringify({
            reasoning: "too expensive",
            action: { kind: "none", description: "Stop" },
            alternatives: [],
            confidence: 0.1,
          }),
          usage: { promptTokens: 6, completionTokens: 5, totalTokens: 11 },
        }),
      ),
    ]);
    const step = createLlmStepFunction({ provider, enableToolCalling: false });

    const result = await step(makeContext(10), makeRuntime());

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("TOKEN_BUDGET_EXCEEDED");
    }
  });

  it("maps invalid tool call arguments to LLM_PARSE_ERROR", async () => {
    const provider = makeProvider([
      ok(
        makeResponse({
          content: null,
          toolCalls: [
            {
              id: "call_1",
              name: "search",
              arguments: "{invalid",
            },
          ],
        }),
      ),
    ]);
    const step = createLlmStepFunction({ provider });

    const result = await step(makeContext(), makeRuntime());

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("LLM_PARSE_ERROR");
    }
  });
});
