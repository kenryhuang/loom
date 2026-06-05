import type { JsonValue } from "../core/json.js";
import type { Result } from "../core/result.js";

export interface LlmMessage {
  readonly role: "system" | "user" | "assistant" | "tool";
  readonly content: string;
  readonly name?: string;
  readonly tool_call_id?: string;
  readonly toolCalls?: readonly LlmToolCall[];
}

export interface LlmTool {
  readonly type: "function";
  readonly function: {
    readonly name: string;
    readonly description: string;
    readonly parameters: JsonValue;
  };
}

export interface LlmResponse {
  readonly content: string | null;
  readonly toolCalls: readonly LlmToolCall[];
  readonly usage: TokenUsage;
  readonly finishReason: string;
}

export interface LlmToolCall {
  readonly id: string;
  readonly name: string;
  readonly arguments: string;
}

export interface TokenUsage {
  readonly promptTokens: number;
  readonly completionTokens: number;
  readonly totalTokens: number;
}

export interface LlmProviderConfig {
  readonly model: string;
  readonly temperature?: number;
  readonly maxTokens?: number;
  readonly apiKey: string;
  readonly baseUrl?: string;
}

export interface LlmProvider {
  readonly model: string;
  chat(
    messages: readonly LlmMessage[],
    tools?: readonly LlmTool[],
    signal?: AbortSignal,
  ): Promise<Result<LlmResponse>>;
}
