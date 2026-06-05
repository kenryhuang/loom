import type { Action, AnyContext, Decision, Observation } from "./context.js";
import type {
  ContextId,
  DurationMs,
  ISODateTime,
  LoopId,
  LoopVersion,
  RunId,
  StepNumber,
  TraceId,
} from "./ids.js";
import type { Immutable } from "./immutable.js";
import type { Metadata } from "./json.js";
import type { LoomError } from "./errors.js";
import type { Result } from "./result.js";

export type TraceOutcome = "pass" | "fail" | "cancelled" | "timeout" | "skipped";

export interface TraceSnapshot<TContext extends AnyContext = AnyContext> {
  readonly contextId: ContextId;
  readonly at: ISODateTime;
  readonly context?: Immutable<TContext>;
  readonly hash?: string;
  readonly metadata?: Metadata;
}

export interface Trace<
  TContext extends AnyContext = AnyContext,
  TObservation extends Observation = Observation,
> {
  readonly id: TraceId;
  readonly runId: RunId;
  readonly loopId: LoopId;
  readonly loopVersion: LoopVersion;
  readonly stepNumber: StepNumber;
  readonly parentTraceId?: TraceId;
  readonly rootTraceId: TraceId;
  readonly startedAt: ISODateTime;
  readonly endedAt: ISODateTime;
  readonly durationMs: DurationMs;
  readonly inputContextId: ContextId;
  readonly outputContextId: ContextId;
  readonly inputSnapshot?: TraceSnapshot<TContext>;
  readonly outputSnapshot?: TraceSnapshot<TContext>;
  readonly outcome: TraceOutcome;
  readonly error?: LoomError;
  readonly observations: readonly TObservation[];
  readonly decisions: readonly Decision[];
  readonly actions: readonly Action[];
  readonly children: readonly TraceId[];
  readonly tags: readonly string[];
  readonly metadata?: Metadata;
}

export type TraceEvent =
  | {
      readonly type: "step.started";
      readonly traceId: TraceId;
      readonly at: ISODateTime;
      readonly contextId: ContextId;
    }
  | {
      readonly type: "decision.recorded";
      readonly traceId: TraceId;
      readonly decision: Decision;
      readonly at: ISODateTime;
    }
  | {
      readonly type: "action.started";
      readonly traceId: TraceId;
      readonly action: Action;
      readonly at: ISODateTime;
    }
  | {
      readonly type: "observation.recorded";
      readonly traceId: TraceId;
      readonly observation: Observation;
      readonly at: ISODateTime;
    }
  | {
      readonly type: "child.started";
      readonly traceId: TraceId;
      readonly childLoopId: LoopId;
      readonly childTraceId: TraceId;
      readonly at: ISODateTime;
    }
  | {
      readonly type: "child.completed";
      readonly traceId: TraceId;
      readonly childTraceId: TraceId;
      readonly outcome: TraceOutcome;
      readonly at: ISODateTime;
    }
  | {
      readonly type: "step.completed";
      readonly trace: Trace;
      readonly at: ISODateTime;
    };

export interface TraceSink {
  emit(event: TraceEvent): Promise<Result<void>>;
}
