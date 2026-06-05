export type Brand<T, Name extends string> = T & { readonly __brand: Name };

export type LoopId = Brand<string, "LoopId">;
export type LoopVersion = Brand<string, "LoopVersion">;
export type ContextId = Brand<string, "ContextId">;
export type TraceId = Brand<string, "TraceId">;
export type RunId = Brand<string, "RunId">;
export type StepNumber = Brand<number, "StepNumber">;
export type ISODateTime = Brand<string, "ISODateTime">;
export type DurationMs = Brand<number, "DurationMs">;

let sequence = 0;

function nextId(prefix: string): string {
  sequence += 1;
  const time = Date.now().toString(36);
  const random = Math.random().toString(36).slice(2, 10);
  return `${prefix}${time}_${sequence.toString(36)}_${random}`;
}

export function newLoopId(): LoopId {
  return nextId("loop_") as LoopId;
}

export function newLoopVersion(): LoopVersion {
  return "v1" as LoopVersion;
}

export function newContextId(): ContextId {
  return nextId("ctx_") as ContextId;
}

export function newTraceId(): TraceId {
  return nextId("trace_") as TraceId;
}

export function newRunId(): RunId {
  return nextId("run_") as RunId;
}

export function asStepNumber(value: number): StepNumber {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(
      `StepNumber must be a non-negative safe integer: ${String(value)}`,
    );
  }

  return value as StepNumber;
}
