import { makeLoomError } from "../../core/errors.js";
import type { JsonValue } from "../../core/json.js";
import { err, ok, type Result } from "../../core/result.js";
import type {
  LlmMessage,
  LlmProvider,
  LlmProviderConfig,
  LlmResponse,
  LlmTool,
  LlmToolCall,
  TokenUsage,
} from "../provider.js";

const defaultBaseUrl = "https://api.openai.com/v1";

type OpenAIMessage = {
  readonly role: LlmMessage["role"];
  readonly content: string | null;
  readonly name?: string;
  readonly tool_call_id?: string;
  readonly tool_calls?: readonly OpenAIToolCall[];
};

interface OpenAIToolCall {
  readonly id: string;
  readonly type: "function";
  readonly function: {
    readonly name: string;
    readonly arguments: string;
  };
}

export function createOpenAIProvider(config: LlmProviderConfig): LlmProvider {
  const baseUrl = normalizeBaseUrl(config.baseUrl);

  return {
    model: config.model,
    chat: async (messages, tools, signal) => {
      const body = {
        model: config.model,
        messages: messages.map(toOpenAIMessage),
        ...(config.temperature === undefined ? {} : { temperature: config.temperature }),
        ...(config.maxTokens === undefined ? {} : { max_tokens: config.maxTokens }),
        ...(tools === undefined || tools.length === 0 ? {} : { tools }),
      };

      const init: RequestInit = {
        method: "POST",
        headers: {
          Authorization: `Bearer ${config.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      };
      const requestInit = signal === undefined ? init : { ...init, signal };

      let response: Response;
      try {
        response = await fetch(`${baseUrl}/chat/completions`, requestInit);
      } catch (error) {
        return llmFailed(messageFromUnknown(error), true, causeFromUnknown(error));
      }

      if (!response.ok) {
        const apiError = await readApiError(response);
        return llmFailed(apiError.message, isRetryableStatus(response.status), {
          status: response.status,
          statusText: response.statusText,
          body: apiError.body,
        });
      }

      let payload: unknown;
      try {
        payload = await response.json();
      } catch (error) {
        return llmFailed("Failed to parse OpenAI response", false, causeFromUnknown(error));
      }

      return parseChatResponse(payload);
    },
  };
}

function normalizeBaseUrl(baseUrl: string | undefined): string {
  const value = baseUrl === undefined || baseUrl.length === 0 ? defaultBaseUrl : baseUrl;
  return value.endsWith("/") ? value.slice(0, -1) : value;
}

function toOpenAIMessage(message: LlmMessage): OpenAIMessage {
  const toolCalls = message.toolCalls;
  const hasToolCalls = toolCalls !== undefined && toolCalls.length > 0;
  const content = message.role === "assistant" && hasToolCalls && message.content.length === 0
    ? null
    : message.content;

  return {
    role: message.role,
    content,
    ...(message.name === undefined ? {} : { name: message.name }),
    ...(message.tool_call_id === undefined ? {} : { tool_call_id: message.tool_call_id }),
    ...(hasToolCalls ? { tool_calls: toolCalls.map(toOpenAIToolCall) } : {}),
  };
}

function toOpenAIToolCall(toolCall: LlmToolCall): OpenAIToolCall {
  return {
    id: toolCall.id,
    type: "function",
    function: {
      name: toolCall.name,
      arguments: toolCall.arguments,
    },
  };
}

function parseChatResponse(payload: unknown): Result<LlmResponse> {
  const root = asRecord(payload);
  const choices = Array.isArray(root?.choices) ? root.choices : undefined;
  const firstChoice = choices?.[0];
  const choice = asRecord(firstChoice);
  const message = asRecord(choice?.message);
  if (choice === undefined || message === undefined) {
    return llmFailed("OpenAI response did not include a chat message", false, toJsonCause(payload));
  }

  const content = typeof message.content === "string" ? message.content : null;
  const toolCalls = parseToolCalls(message.tool_calls);
  const usage = parseUsage(root?.usage);
  const finishReason = typeof choice.finish_reason === "string" ? choice.finish_reason : "unknown";

  return ok({
    content,
    toolCalls,
    usage,
    finishReason,
  });
}

function parseToolCalls(value: unknown): readonly LlmToolCall[] {
  if (!Array.isArray(value)) {
    return [];
  }

  const calls: LlmToolCall[] = [];
  for (const item of value) {
    const call = asRecord(item);
    const fn = asRecord(call?.function);
    if (call === undefined || fn === undefined) {
      continue;
    }
    if (typeof call.id !== "string" || typeof fn.name !== "string") {
      continue;
    }
    calls.push({
      id: call.id,
      name: fn.name,
      arguments: typeof fn.arguments === "string" ? fn.arguments : "{}",
    });
  }
  return calls;
}

function parseUsage(value: unknown): TokenUsage {
  const usage = asRecord(value);
  return {
    promptTokens: numberOrZero(usage?.prompt_tokens),
    completionTokens: numberOrZero(usage?.completion_tokens),
    totalTokens: numberOrZero(usage?.total_tokens),
  };
}

function numberOrZero(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

async function readApiError(response: Response): Promise<{ readonly message: string; readonly body: JsonValue }> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = response.statusText;
  }

  const record = asRecord(body);
  const error = asRecord(record?.error);
  const message = typeof error?.message === "string"
    ? error.message
    : `OpenAI request failed with status ${String(response.status)}`;

  return {
    message,
    body: toJsonCause(body),
  };
}

function isRetryableStatus(status: number): boolean {
  return status === 429 || status >= 500;
}

function llmFailed<T>(message: string, retryable: boolean, cause?: JsonValue): Result<T> {
  return err(
    makeLoomError({
      code: "LLM_FAILED",
      message,
      retryable,
      ...(cause === undefined ? {} : { cause }),
    }),
  );
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : undefined;
}

function messageFromUnknown(value: unknown): string {
  if (value instanceof Error) {
    return value.message;
  }
  if (typeof value === "string") {
    return value;
  }
  return "OpenAI request failed";
}

function causeFromUnknown(value: unknown): JsonValue {
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
    };
  }
  return toJsonCause(value);
}

function toJsonCause(value: unknown): JsonValue {
  if (isJsonValue(value)) {
    return value;
  }
  return { value: Object.prototype.toString.call(value) };
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
