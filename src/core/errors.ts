import type { TraceId } from "./ids.js";
import type { JsonValue, Metadata } from "./json.js";

export type LoomErrorCode =
  | "ABORTED"
  | "TIMEOUT"
  | "BUDGET_EXCEEDED"
  | "VALIDATION_FAILED"
  | "TOOL_FAILED"
  | "LLM_FAILED"
  | "LLM_PARSE_ERROR"
  | "TOKEN_BUDGET_EXCEEDED"
  | "LOOP_FAILED"
  | "MERGE_CONFLICT"
  | "MUTATION_REJECTED"
  | "SERIALIZATION_FAILED"
  | "INTERNAL";

export interface LoomError {
  readonly code: LoomErrorCode;
  readonly message: string;
  readonly retryable: boolean;
  readonly traceId?: TraceId;
  readonly cause?: JsonValue;
  readonly metadata?: Metadata;
}

export interface MakeLoomErrorInput {
  readonly code: LoomErrorCode;
  readonly message: string;
  readonly retryable: boolean;
  readonly traceId?: TraceId;
  readonly cause?: JsonValue;
  readonly metadata?: Metadata;
}

const loomErrorCodes = new Set<string>([
  "ABORTED",
  "TIMEOUT",
  "BUDGET_EXCEEDED",
  "VALIDATION_FAILED",
  "TOOL_FAILED",
  "LLM_FAILED",
  "LLM_PARSE_ERROR",
  "TOKEN_BUDGET_EXCEEDED",
  "LOOP_FAILED",
  "MERGE_CONFLICT",
  "MUTATION_REJECTED",
  "SERIALIZATION_FAILED",
  "INTERNAL",
]);

export function makeLoomError(input: MakeLoomErrorInput): LoomError {
  const base = {
    code: input.code,
    message: input.message,
    retryable: input.retryable,
  };

  return {
    ...base,
    ...(input.traceId === undefined ? {} : { traceId: input.traceId }),
    ...(input.cause === undefined ? {} : { cause: input.cause }),
    ...(input.metadata === undefined ? {} : { metadata: input.metadata }),
  };
}

export function toLoomError(error: unknown): LoomError {
  if (isLoomError(error)) {
    return error;
  }

  return makeLoomError({
    code: "INTERNAL",
    message: messageFromUnknown(error),
    retryable: false,
    cause: causeFromUnknown(error),
  });
}

function isLoomError(value: unknown): value is LoomError {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as {
    readonly code?: unknown;
    readonly message?: unknown;
    readonly retryable?: unknown;
  };

  return (
    typeof candidate.code === "string" &&
    loomErrorCodes.has(candidate.code) &&
    typeof candidate.message === "string" &&
    typeof candidate.retryable === "boolean"
  );
}

function messageFromUnknown(value: unknown): string {
  if (value instanceof Error) {
    return value.message;
  }

  if (typeof value === "string") {
    return value;
  }

  return "Internal error";
}

function causeFromUnknown(value: unknown): JsonValue {
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
    };
  }

  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return value;
  }

  return { value: Object.prototype.toString.call(value) };
}
