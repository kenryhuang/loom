import type {
  AnyContext,
  ContextPatch,
  EvaluatorRef,
  GoalLayer,
  IdentityLayer,
  ImplementationRef,
  Observation,
  SuccessCriterion,
  ToolRef,
} from "./context.js";
import type { LoopId, LoopVersion, RunId, TraceId, ISODateTime, DurationMs } from "./ids.js";
import type { Immutable, MaybePromise } from "./immutable.js";
import type { JsonValue, Metadata } from "./json.js";
import type { Result } from "./result.js";
import type { Trace, TraceEvent, TraceOutcome, TraceSink } from "./trace.js";

export type RuntimeTrace = Trace;
export type RuntimeTraceEvent = TraceEvent;
export type RuntimeTraceOutcome = TraceOutcome;

export interface MinimalLoopDefinition<
  TContext extends AnyContext = AnyContext,
  TObservation extends Observation = Observation,
  TOutput extends JsonValue = JsonValue,
> {
  readonly id: LoopId;
  readonly version: LoopVersion;
  readonly identity: Immutable<IdentityLayer>;
  readonly goal: Immutable<GoalLayer>;
  readonly step: StepFunction<TContext, TObservation, TOutput>;
  readonly done: DoneFunction<TContext>;
  readonly metadata?: Metadata;
}

export interface TraceReader {
  query(options?: TraceOptions): AsyncIterable<Trace>;
  get(id: TraceId): Promise<Result<Trace | undefined>>;
}

export interface LoopHandle<
  TContext extends AnyContext = AnyContext,
  TObservation extends Observation = Observation,
  TOutput extends JsonValue = JsonValue,
> {
  readonly id: LoopId;
  readonly version: LoopVersion;
  readonly definition: MinimalLoopDefinition<TContext, TObservation, TOutput>;
  readonly traceReader: TraceReader;
  readonly createdAt: ISODateTime;
}

export interface StepResult<
  TContext extends AnyContext = AnyContext,
  TObservation extends Observation = Observation,
  TOutput extends JsonValue = JsonValue,
> {
  readonly context: TContext;
  readonly trace: Trace<TContext, TObservation>;
  readonly observation?: TObservation;
  readonly output?: TOutput;
}

export type StepFunction<
  TContext extends AnyContext = AnyContext,
  TObservation extends Observation = Observation,
  TOutput extends JsonValue = JsonValue,
> = (
  context: TContext,
  runtime: StepRuntime<TContext>,
) => Promise<Result<StepResult<TContext, TObservation, TOutput>>>;

export type DoneFunction<TContext extends AnyContext = AnyContext> = (
  context: TContext,
  runtime: DoneRuntime,
) => MaybePromise<Result<boolean>>;

export interface RuntimeRegistry {
  readonly tools: ToolRegistry;
  readonly loops: LoopRegistryView;
  readonly evaluators: EvaluatorRegistry;
  readonly implementations: ImplementationRegistry;
}

export interface ToolRegistry {
  get(toolId: string): Result<ToolHandler>;
}

export interface ToolHandler {
  readonly ref: ToolRef;
  invoke(input: JsonValue, options: ToolCallOptions): Promise<Result<Observation>>;
}

export interface LoopRegistryView {
  get(loopId: LoopId, version?: LoopVersion): Result<LoopHandle>;
}

export interface EvaluatorRegistry {
  get(ref: EvaluatorRef): Result<CriterionEvaluator>;
}

export interface CriterionEvaluator {
  evaluate(
    context: AnyContext,
    criterion: SuccessCriterion,
    options?: DoneOptions,
  ): Promise<Result<boolean>>;
}

export interface ImplementationRegistry {
  get(ref: ImplementationRef): Result<StepFunction | DoneFunction>;
}

export interface StepRuntime<TContext extends AnyContext = AnyContext> {
  readonly runId: RunId;
  readonly loopId: LoopId;
  readonly signal: AbortSignal;
  readonly registry: RuntimeRegistry;
  readonly traceSink: TraceSink;
  readonly now: () => ISODateTime;
  readonly applyPatch: (context: TContext, patch: ContextPatch<TContext>) => Result<TContext>;
  readonly callTool: (
    toolId: string,
    input: JsonValue,
    options?: ToolCallOptions,
  ) => Promise<Result<Observation>>;
  readonly runLoop: (
    loopId: LoopId,
    context: AnyContext,
    options?: RunOptions,
  ) => Promise<Result<RunResult>>;
}

export interface DoneRuntime {
  readonly signal: AbortSignal;
  readonly now: () => ISODateTime;
  readonly registry: RuntimeRegistry;
}

export interface ToolCallOptions {
  readonly signal?: AbortSignal;
  readonly timeoutMs?: number;
  readonly metadata?: Metadata;
}

export interface RunOptions {
  readonly signal?: AbortSignal;
  readonly timeoutMs?: number;
  readonly maxSteps?: number;
  readonly traceSink?: TraceSink;
  readonly metadata?: Metadata;
}

export interface RunMetrics {
  readonly steps: number;
  readonly startedAt: ISODateTime;
  readonly endedAt: ISODateTime;
  readonly durationMs: DurationMs;
  readonly traceCount: number;
  readonly outcome: RuntimeTraceOutcome;
}

export interface RunResult<
  TContext extends AnyContext = AnyContext,
  TOutput extends JsonValue = JsonValue,
> {
  readonly context: TContext;
  readonly traces: readonly RuntimeTrace[];
  readonly output?: TOutput;
  readonly metrics: RunMetrics;
}

export interface CreateOptions {
  readonly runId?: RunId;
  readonly initialContext?: AnyContext;
  readonly traceStore?: unknown;
  readonly registry?: RuntimeRegistry;
  readonly metadata?: Metadata;
}

export interface StepOptions {
  readonly signal?: AbortSignal;
  readonly timeoutMs?: number;
  readonly traceSink?: TraceSink;
  readonly metadata?: Metadata;
}

export interface DoneOptions {
  readonly signal?: AbortSignal;
  readonly timeoutMs?: number;
}

export interface TraceOptions {
  readonly runId?: RunId;
  readonly loopId?: LoopId;
  readonly rootTraceId?: TraceId;
  readonly parentTraceId?: TraceId;
  readonly outcome?: readonly TraceOutcome[];
  readonly tags?: readonly string[];
  readonly fromStep?: number;
  readonly toStep?: number;
  readonly includeSnapshots?: boolean;
  readonly limit?: number;
}
