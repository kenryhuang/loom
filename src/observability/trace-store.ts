import type { LoopId, RunId, TraceId } from "../core/ids.js";
import type { Result } from "../core/result.js";
import type { Trace, TraceEvent, TraceOutcome } from "../core/trace.js";

export interface TraceQuery {
  readonly runId?: RunId;
  readonly loopId?: LoopId;
  readonly rootTraceId?: TraceId;
  readonly parentTraceId?: TraceId;
  readonly outcome?: readonly TraceOutcome[];
  readonly tags?: readonly string[];
  readonly limit?: number;
}

export interface TraceStore {
  append(trace: Trace): Promise<Result<void>>;
  appendEvent(event: TraceEvent): Promise<Result<void>>;
  get(id: TraceId): Promise<Result<Trace>>;
  query(query: TraceQuery): AsyncIterable<Trace>;
  children(id: TraceId): AsyncIterable<Trace>;
}
