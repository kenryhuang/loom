import {
  asStepNumber,
  newContextId,
  newLoopVersion,
  newTraceId,
  type DurationMs,
  type ISODateTime,
} from "../core/ids.js";
import { makeLoomError } from "../core/errors.js";
import {
  freezeContext,
  type Action,
  type AnyContext,
  type Decision,
  type Observation,
} from "../core/context.js";
import type { JsonValue, Metadata } from "../core/json.js";
import type { StepFunction, StepRuntime, StepResult } from "../core/loop.js";
import { err, ok, type Result } from "../core/result.js";
import type { Trace, TraceEvent } from "../core/trace.js";
import { buildMessages, type PromptBuilderOptions } from "./prompt-builder.js";
import type { LlmMessage, LlmProvider, LlmResponse, LlmToolCall, TokenUsage } from "./provider.js";
import { createTokenTracker } from "./token-tracker.js";
import { toLlmTools } from "./tools.js";

export interface LlmStepConfig {
  readonly provider: LlmProvider;
  readonly promptOptions?: PromptBuilderOptions;
  readonly enableToolCalling?: boolean;
  readonly maxToolCallsPerStep?: number;
}

interface ParsedDecision {
  readonly output: JsonValue;
  readonly reasoning: string;
  readonly action: Action;
  readonly alternatives: readonly Action[];
  readonly confidence: number;
  readonly parseFallback: boolean;
}

const zeroDuration = 0 as DurationMs;

export function createLlmStepFunction(
  config: LlmStepConfig,
): StepFunction<AnyContext, Observation, JsonValue> {
  return async (context, runtime) => {
    const startedAtMs = Date.now();
    const startedAt = runtime.now();
    const traceId = newTraceId();
    const tokenTracker = createTokenTracker();
    const toolCallingEnabled =
      config.enableToolCalling === undefined ? true : config.enableToolCalling;
    const maxToolCalls =
      config.maxToolCallsPerStep === undefined ? 5 : config.maxToolCallsPerStep;
    const tools =
      toolCallingEnabled && context.affordances.tools.length > 0
        ? toLlmTools(context.affordances.tools)
        : undefined;
    let messages = [...buildMessages(context, config.promptOptions)];
    let finalResponse: LlmResponse | undefined;
    let toolCallCount = 0;
    const toolObservations: Observation[] = [];

    for (;;) {
      const response = await config.provider.chat(messages, tools, runtime.signal);
      if (!response.ok) {
        return err(response.error);
      }

      finalResponse = response.value;
      tokenTracker.add(response.value.usage);
      if (!tokenTracker.isWithinBudget(context.goal.budget.maxTokens)) {
        return tokenBudgetExceeded(tokenTracker.total, context.goal.budget.maxTokens);
      }

      if (!toolCallingEnabled || response.value.toolCalls.length === 0) {
        break;
      }

      if (toolCallCount >= maxToolCalls) {
        return maxToolCallsExceeded(maxToolCalls);
      }

      messages = [...messages, assistantMessageFromResponse(response.value)];

      for (const toolCall of response.value.toolCalls) {
        if (toolCallCount >= maxToolCalls) {
          return maxToolCallsExceeded(maxToolCalls);
        }

        const input = parseToolArguments(toolCall);
        if (!input.ok) {
          return input;
        }

        const observation = await runtime.callTool(toolCall.name, input.value, {
          signal: runtime.signal,
          metadata: {
            toolCallId: toolCall.id,
            toolName: toolCall.name,
            model: config.provider.model,
          },
        });
        if (!observation.ok) {
          return err(observation.error);
        }

        toolCallCount += 1;
        toolObservations.push(observation.value);
        messages = [...messages, toolMessageFromObservation(toolCall, observation.value)];
      }
    }

    if (finalResponse === undefined) {
      return llmFailed("LLM provider returned no response", false);
    }

    const parsed = parseDecision(finalResponse.content, traceId);
    const endedAt = runtime.now();
    const llmObservation = makeLlmObservation(
      traceId,
      parsed.output,
      endedAt,
      config.provider.model,
      finalResponse.finishReason,
      tokenTracker.total,
    );
    const decision = makeDecision(
      traceId,
      parsed,
      endedAt,
      config.provider.model,
      finalResponse.finishReason,
      tokenTracker.total,
    );
    const observations = [...toolObservations, llmObservation];
    const nextContext = freezeContext({
      ...context,
      id: newContextId(),
      state: {
        ...context.state,
        observations: [...context.state.observations, ...observations],
        decisions: [...context.state.decisions, decision],
      },
    });
    const trace = makeTrace({
      context,
      nextContext,
      runtime,
      traceId,
      startedAt,
      startedAtMs,
      observations,
      decision,
      tokenUsage: tokenTracker.total,
      model: config.provider.model,
      finishReason: finalResponse.finishReason,
    });

    const emitted = await emitStepEvents(runtime, trace, observations, decision);
    if (!emitted.ok) {
      return err(emitted.error);
    }

    return ok({
      context: nextContext,
      trace,
      observation: llmObservation,
      output: parsed.output,
    } satisfies StepResult<AnyContext, Observation, JsonValue>);
  };
}

function assistantMessageFromResponse(response: LlmResponse): LlmMessage {
  return {
    role: "assistant",
    content: response.content === null ? "" : response.content,
    toolCalls: response.toolCalls,
  };
}

function toolMessageFromObservation(toolCall: LlmToolCall, observation: Observation): LlmMessage {
  return {
    role: "tool",
    content: JSON.stringify(observation.value),
    name: toolCall.name,
    tool_call_id: toolCall.id,
  };
}

function parseToolArguments(toolCall: LlmToolCall): Result<JsonValue> {
  try {
    const parsed = JSON.parse(toolCall.arguments) as unknown;
    if (!isJsonValue(parsed)) {
      return llmParseError(`Tool call ${toolCall.id} arguments are not JSON-compatible`, {
        toolCallId: toolCall.id,
      });
    }
    return ok(parsed);
  } catch (error) {
    return llmParseError(`Failed to parse tool call ${toolCall.id} arguments`, {
      toolCallId: toolCall.id,
      cause: error instanceof Error ? error.message : String(error),
    });
  }
}

function parseDecision(content: string | null, traceId: ReturnType<typeof newTraceId>): ParsedDecision {
  if (content === null || content.trim().length === 0) {
    return fallbackDecision("LLM returned no decision content", traceId);
  }

  try {
    const parsed = JSON.parse(content) as unknown;
    if (!isJsonValue(parsed)) {
      return fallbackDecision(content, traceId);
    }
    const record = asRecord(parsed);
    if (record === undefined) {
      return fallbackDecision(content, traceId);
    }

    const reasoning = typeof record.reasoning === "string" ? record.reasoning : content;
    const action = parseAction(record.action, `${traceId}-action`, "LLM selected action");
    const alternatives = Array.isArray(record.alternatives)
      ? record.alternatives.map((item, index) =>
          parseAction(item, `${traceId}-alternative-${String(index + 1)}`, "Alternative action"),
        )
      : [];
    const confidence =
      typeof record.confidence === "number" && Number.isFinite(record.confidence)
        ? clamp(record.confidence, 0, 1)
        : 0;

    return {
      output: parsed,
      reasoning,
      action,
      alternatives,
      confidence,
      parseFallback: false,
    };
  } catch {
    return fallbackDecision(content, traceId);
  }
}

function fallbackDecision(reasoning: string, traceId: ReturnType<typeof newTraceId>): ParsedDecision {
  const action: Action = {
    id: `${traceId}-action`,
    kind: "custom",
    description: "Use unstructured LLM response",
    input: { content: reasoning },
  };
  const outputAction: JsonValue = {
    kind: action.kind,
    description: action.description,
    ...(action.input === undefined ? {} : { input: action.input }),
  };
  return {
    output: {
      reasoning,
      action: outputAction,
      alternatives: [],
      confidence: 0,
    },
    reasoning,
    action,
    alternatives: [],
    confidence: 0,
    parseFallback: true,
  };
}

function parseAction(value: unknown, id: string, fallbackDescription: string): Action {
  const record = asRecord(value);
  if (record === undefined) {
    return {
      id,
      kind: "custom",
      description: fallbackDescription,
    };
  }

  const kind = isActionKind(record.kind) ? record.kind : "custom";
  const description =
    typeof record.description === "string" && record.description.length > 0
      ? record.description
      : fallbackDescription;
  const input = isJsonValue(record.input) ? record.input : undefined;
  const target = typeof record.target === "string" ? record.target : undefined;

  return {
    id,
    kind,
    description,
    ...(input === undefined ? {} : { input }),
    ...(target === undefined ? {} : { target }),
  };
}

function isActionKind(value: unknown): value is Action["kind"] {
  return (
    value === "tool" ||
    value === "loop" ||
    value === "context" ||
    value === "knowledge" ||
    value === "none" ||
    value === "custom"
  );
}

function makeLlmObservation(
  traceId: ReturnType<typeof newTraceId>,
  value: JsonValue,
  at: ISODateTime,
  model: string,
  finishReason: string,
  tokenUsage: TokenUsage,
): Observation {
  return {
    id: `${traceId}-llm-observation`,
    source: "llm",
    value,
    at,
    metadata: {
      model,
      finishReason,
      tokenUsage: tokenUsageMetadata(tokenUsage),
    },
  };
}

function makeDecision(
  traceId: ReturnType<typeof newTraceId>,
  parsed: ParsedDecision,
  at: ISODateTime,
  model: string,
  finishReason: string,
  tokenUsage: TokenUsage,
): Decision {
  return {
    id: `${traceId}-decision`,
    action: parsed.action,
    reasoning: parsed.reasoning,
    alternatives: parsed.alternatives,
    confidence: parsed.confidence,
    at,
    metadata: {
      model,
      finishReason,
      parseFallback: parsed.parseFallback,
      tokenUsage: tokenUsageMetadata(tokenUsage),
    },
  };
}

function makeTrace(input: {
  readonly context: AnyContext;
  readonly nextContext: AnyContext;
  readonly runtime: StepRuntime<AnyContext>;
  readonly traceId: ReturnType<typeof newTraceId>;
  readonly startedAt: ISODateTime;
  readonly startedAtMs: number;
  readonly observations: readonly Observation[];
  readonly decision: Decision;
  readonly tokenUsage: TokenUsage;
  readonly model: string;
  readonly finishReason: string;
}): Trace<AnyContext, Observation> {
  const endedAtMs = Date.now();
  return {
    id: input.traceId,
    runId: input.context.runId,
    loopId: input.runtime.loopId,
    loopVersion: newLoopVersion(),
    stepNumber: asStepNumber(input.context.state.observations.length),
    rootTraceId: input.traceId,
    startedAt: input.startedAt,
    endedAt: input.runtime.now(),
    durationMs: Math.max(0, endedAtMs - input.startedAtMs) as DurationMs,
    inputContextId: input.context.id,
    outputContextId: input.nextContext.id,
    outcome: "pass",
    observations: input.observations,
    decisions: [input.decision],
    actions: [input.decision.action],
    children: [],
    tags: ["llm"],
    metadata: {
      model: input.model,
      finishReason: input.finishReason,
      tokenUsage: tokenUsageMetadata(input.tokenUsage),
    },
  };
}

async function emitStepEvents(
  runtime: StepRuntime<AnyContext>,
  trace: Trace<AnyContext, Observation>,
  observations: readonly Observation[],
  decision: Decision,
): Promise<Result<void>> {
  const events: TraceEvent[] = [
    {
      type: "decision.recorded",
      traceId: trace.id,
      decision,
      at: decision.at,
    },
    {
      type: "action.started",
      traceId: trace.id,
      action: decision.action,
      at: decision.at,
    },
    ...observations.map((observation) => ({
      type: "observation.recorded" as const,
      traceId: trace.id,
      observation,
      at: observation.at,
    })),
  ];

  for (const event of events) {
    const emitted = await runtime.traceSink.emit(event);
    if (!emitted.ok) {
      return emitted;
    }
  }
  return ok(undefined);
}

function tokenUsageMetadata(usage: TokenUsage): Metadata {
  return {
    promptTokens: usage.promptTokens,
    completionTokens: usage.completionTokens,
    totalTokens: usage.totalTokens,
  };
}

function tokenBudgetExceeded<T>(usage: TokenUsage, maxTokens: number | undefined): Result<T> {
  return err(
    makeLoomError({
      code: "TOKEN_BUDGET_EXCEEDED",
      message: "LLM token budget exceeded",
      retryable: false,
      cause: {
        maxTokens: maxTokens === undefined ? null : maxTokens,
        usage: tokenUsageMetadata(usage),
      },
    }),
  );
}

function maxToolCallsExceeded<T>(maxToolCalls: number): Result<T> {
  return err(
    makeLoomError({
      code: "LLM_FAILED",
      message: "Maximum LLM tool calls per step exceeded",
      retryable: false,
      cause: {
        maxToolCalls,
      },
    }),
  );
}

function llmFailed<T>(message: string, retryable: boolean): Result<T> {
  return err(
    makeLoomError({
      code: "LLM_FAILED",
      message,
      retryable,
    }),
  );
}

function llmParseError<T>(message: string, cause: JsonValue): Result<T> {
  return err(
    makeLoomError({
      code: "LLM_PARSE_ERROR",
      message,
      retryable: false,
      cause,
    }),
  );
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : undefined;
}

function isJsonValue(value: unknown): value is JsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return true;
  }
  if (Array.isArray(value)) {
    return value.every(isJsonValue);
  }
  if (typeof value !== "object") {
    return false;
  }
  for (const item of Object.values(value as Record<string, unknown>)) {
    if (!isJsonValue(item)) {
      return false;
    }
  }
  return true;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
