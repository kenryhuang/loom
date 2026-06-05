# Loom Detailed Design

> Implementation-oriented design for Loom, derived from `design.md`.
> Core axiom: **Everything is a Loop.**

This document turns the high-level model into concrete TypeScript contracts, runtime algorithms, storage abstractions, and phased implementation criteria. The terminology intentionally follows `design.md`: MinimalLoop, Context, Trace, chain, nest, fork, meta, and Level 1/2/3 evolution.

## 1. 核心类型定义

### 1.1 基础类型

Loom 的核心对象必须可以被 trace、序列化、版本化和回放。因此公共数据结构默认使用 JSON-compatible value。函数、tool handler、storage connection 等不可序列化对象通过 registry 引用进入系统，而不直接进入 Context snapshot。

```typescript
export type Brand<T, Name extends string> = T & { readonly __brand: Name };

export type LoopId = Brand<string, "LoopId">;
export type LoopVersion = Brand<string, "LoopVersion">;
export type ContextId = Brand<string, "ContextId">;
export type TraceId = Brand<string, "TraceId">;
export type RunId = Brand<string, "RunId">;
export type StepNumber = Brand<number, "StepNumber">;
export type ISODateTime = Brand<string, "ISODateTime">;
export type DurationMs = Brand<number, "DurationMs">;

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  | JsonPrimitive
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };

export type Metadata = Readonly<Record<string, JsonValue>>;

export type Immutable<T> =
  T extends (...args: readonly never[]) => unknown ? T :
  T extends JsonPrimitive ? T :
  T extends readonly (infer U)[] ? readonly Immutable<U>[] :
  T extends Map<infer K, infer V> ? ReadonlyMap<Immutable<K>, Immutable<V>> :
  T extends Set<infer U> ? ReadonlySet<Immutable<U>> :
  { readonly [K in keyof T]: Immutable<T[K]> };

export type MaybePromise<T> = T | Promise<T>;
```

**为什么：**

- `Brand<T, Name>` 避免把任意 string 误传成 `LoopId` 或 `TraceId`。
- `JsonValue` 限制可持久化边界，保证 trace 和 context snapshot 可以稳定落盘。
- `Immutable<T>` 是编译期约束，运行时再用 `deepFreeze()` 或持久化数据结构补强。

### 1.2 Identity / Goal / State / Knowledge / Affordances

Context 的五层是 Loom 的统一载体。`identity` 和 `goal` 是必填层；`state`、`knowledge`、`affordances` 也必填，但可以为空集合。这样 runtime 不需要处理缺层分支。

```typescript
export interface Constraint {
  readonly id: string;
  readonly description: string;
  readonly severity: "must" | "should" | "may";
  readonly metadata?: Metadata;
}

export interface Capability {
  readonly id: string;
  readonly description: string;
  readonly inputSchema?: JsonValue;
  readonly outputSchema?: JsonValue;
  readonly metadata?: Metadata;
}

export interface IdentityLayer {
  readonly role: string;
  readonly capabilities: readonly Capability[];
  readonly constraints: readonly Constraint[];
  readonly metadata?: Metadata;
}

export interface SuccessCriterion {
  readonly id: string;
  readonly description: string;
  readonly evaluator?: EvaluatorRef;
  readonly required: boolean;
  readonly metadata?: Metadata;
}

export interface Budget {
  readonly maxSteps?: number;
  readonly maxDurationMs?: number;
  readonly maxTokens?: number;
  readonly maxCostUsd?: number;
}

export interface GoalLayer {
  readonly objective: string;
  readonly criteria: readonly SuccessCriterion[];
  readonly budget: Budget;
  readonly parentGoalId?: string;
  readonly metadata?: Metadata;
}

export interface Observation {
  readonly id: string;
  readonly source: string;
  readonly value: JsonValue;
  readonly at: ISODateTime;
  readonly metadata?: Metadata;
}

export interface Decision {
  readonly id: string;
  readonly action: Action;
  readonly reasoning: string;
  readonly alternatives: readonly Action[];
  readonly confidence: number;
  readonly at: ISODateTime;
  readonly metadata?: Metadata;
}

export interface PendingLoop {
  readonly id: string;
  readonly loopId: LoopId;
  readonly goal: GoalLayer;
  readonly startedAt: ISODateTime;
  readonly traceId?: TraceId;
  readonly metadata?: Metadata;
}

export interface StateLayer<TObservation extends Observation = Observation> {
  readonly observations: readonly TObservation[];
  readonly decisions: readonly Decision[];
  readonly pending: readonly PendingLoop[];
  readonly scratch?: Metadata;
}

export interface KnowledgeItem {
  readonly id: string;
  readonly kind: "fact" | "heuristic" | "memory";
  readonly content: JsonValue;
  readonly sourceTraceId?: TraceId;
  readonly confidence: number;
  readonly createdAt: ISODateTime;
  readonly updatedAt?: ISODateTime;
  readonly metadata?: Metadata;
}

export interface KnowledgeLayer {
  readonly facts: readonly KnowledgeItem[];
  readonly heuristics: readonly KnowledgeItem[];
  readonly memories: readonly KnowledgeItem[];
  readonly version: string;
  readonly metadata?: Metadata;
}

export interface ToolRef {
  readonly id: string;
  readonly description: string;
  readonly inputSchema?: JsonValue;
  readonly outputSchema?: JsonValue;
  readonly timeoutMs?: number;
  readonly metadata?: Metadata;
}

export interface LoopRef {
  readonly loopId: LoopId;
  readonly version?: LoopVersion;
  readonly description: string;
  readonly inputProjection?: ProjectionRef;
  readonly metadata?: Metadata;
}

export interface ResourceRef {
  readonly id: string;
  readonly kind: "file" | "directory" | "api" | "database" | "memory" | "environment";
  readonly uri: string;
  readonly access: "read" | "write" | "readwrite";
  readonly metadata?: Metadata;
}

export interface AffordanceLayer {
  readonly tools: readonly ToolRef[];
  readonly loops: readonly LoopRef[];
  readonly resources: readonly ResourceRef[];
  readonly metadata?: Metadata;
}

export type EvaluatorRef = Brand<string, "EvaluatorRef">;
export type ProjectionRef = Brand<string, "ProjectionRef">;
export type ImplementationRef = Brand<string, "ImplementationRef">;
```

**为什么：**

- `state.observations` 用泛型参数化，允许具体 loop 定义强类型 observation。
- `knowledge` 使用 `KnowledgeItem.kind` 统一索引，同时保留 `facts/heuristics/memories` 三个语义集合。
- `affordances` 只放引用，不放实际函数，避免 snapshot 无法序列化。

### 1.3 Context 泛型设计

`Context` 需要在框架层保持统一，同时允许业务 loop 对 `state` 和 `observation` 做强类型约束。最小泛型入口是 state 和 observation；knowledge 与 affordances 默认使用标准层，只有高级场景才扩展。

```typescript
export interface Context<
  TObservation extends Observation = Observation,
  TState extends StateLayer<TObservation> = StateLayer<TObservation>,
  TKnowledge extends KnowledgeLayer = KnowledgeLayer,
  TAffordances extends AffordanceLayer = AffordanceLayer,
> {
  readonly id: ContextId;
  readonly runId: RunId;
  readonly createdAt: ISODateTime;
  readonly identity: Immutable<IdentityLayer>;
  readonly goal: Immutable<GoalLayer>;
  readonly state: Immutable<TState>;
  readonly knowledge: Immutable<TKnowledge>;
  readonly affordances: Immutable<TAffordances>;
  readonly parentContextId?: ContextId;
  readonly metadata?: Metadata;
}

export type AnyContext = Context<Observation, StateLayer<Observation>, KnowledgeLayer, AffordanceLayer>;

export interface ContextPatch<TContext extends AnyContext = AnyContext> {
  readonly baseContextId: ContextId;
  readonly operations: readonly PatchOperation[];
  readonly reason: string;
  readonly metadata?: Metadata;
}

export type PatchOperation =
  | { readonly op: "appendObservation"; readonly value: Observation }
  | { readonly op: "appendDecision"; readonly value: Decision }
  | { readonly op: "clearPending"; readonly id: string }
  | { readonly op: "appendPending"; readonly value: PendingLoop }
  | { readonly op: "addKnowledge"; readonly value: KnowledgeItem }
  | { readonly op: "replaceGoal"; readonly value: GoalLayer }
  | { readonly op: "replaceIdentity"; readonly value: IdentityLayer }
  | { readonly op: "replaceAffordances"; readonly value: AffordanceLayer }
  | { readonly op: "setMetadata"; readonly key: string; readonly value: JsonValue };
```

**字段必填性：**

- 必填：`id`, `runId`, `createdAt`, `identity`, `goal`, `state`, `knowledge`, `affordances`。
- 可选：`parentContextId`, `metadata`。
- `budget` 必填但内部字段可选。空 budget 表示没有对应限制。

**为什么：**

- 每个 Context 都有 `id` 和 `runId`，trace 可以只存引用或 delta。
- 使用 `ContextPatch` 而不是原地写入，保证历史 snapshot 一致。
- `replaceIdentity/replaceGoal/replaceAffordances` 只允许在显式 patch 中出现，避免普通 step 悄悄改变边界条件。

### 1.4 Action / Result / Error

所有预期失败用 `Result<T, LoomError>` 返回；只有编程错误和 invariant 破坏才 throw。

```typescript
export interface Action {
  readonly id: string;
  readonly kind: "tool" | "loop" | "context" | "knowledge" | "none" | "custom";
  readonly description: string;
  readonly input?: JsonValue;
  readonly target?: string;
  readonly metadata?: Metadata;
}

export type Result<T, E extends LoomError = LoomError> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: E };

export interface LoomError {
  readonly code:
    | "ABORTED"
    | "TIMEOUT"
    | "BUDGET_EXCEEDED"
    | "VALIDATION_FAILED"
    | "TOOL_FAILED"
    | "LOOP_FAILED"
    | "MERGE_CONFLICT"
    | "MUTATION_REJECTED"
    | "SERIALIZATION_FAILED"
    | "INTERNAL";
  readonly message: string;
  readonly retryable: boolean;
  readonly traceId?: TraceId;
  readonly cause?: JsonValue;
  readonly metadata?: Metadata;
}
```

**为什么：**

- 组合模式需要可预测地传播失败；`Result` 比 throw 更容易收集、合并和记录。
- `retryable` 让 runtime 和 evolution engine 可以区分临时失败与设计缺陷。

### 1.5 MinimalLoop 类型

MinimalLoop 的四要素保留为核心抽象。实现上将 loop 的不可变定义和运行时 handle 分开。

```typescript
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
```

**为什么：**

- `MinimalLoopDefinition` 是版本化、不可变的；meta mutation 不直接改它，而是产生新版本。
- `LoopHandle` 绑定 trace reader 和创建时间，代表一次可运行实例。
- `step()` 固定 async。tool call、子 loop、外部资源访问都会异步，统一 async 可以避免 sync/async 双轨复杂度。

## 2. 统一接口的具体 API 设计

### 2.1 create / step / done / trace

公共 API 保持 `design.md` 的四个操作，但 TypeScript 签名需要包含 runtime options、取消、超时和 trace streaming。

```typescript
export interface CreateOptions {
  readonly runId?: RunId;
  readonly initialContext?: AnyContext;
  readonly traceStore?: TraceStore;
  readonly registry?: RuntimeRegistry;
  readonly metadata?: Metadata;
}

export interface StepOptions {
  readonly signal?: AbortSignal;
  readonly timeoutMs?: number;
  readonly traceSink?: TraceSink;
  readonly samplePolicy?: TraceSamplePolicy;
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
  readonly fromStep?: number;
  readonly toStep?: number;
  readonly includeSnapshots?: boolean;
  readonly limit?: number;
}

export function create<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(
  definition: MinimalLoopDefinition<TContext, TObservation, TOutput>,
  options?: CreateOptions,
): Result<LoopHandle<TContext, TObservation, TOutput>>;

export function step<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(
  loop: LoopHandle<TContext, TObservation, TOutput>,
  context: TContext,
  options?: StepOptions,
): Promise<Result<StepResult<TContext, TObservation, TOutput>>>;

export function done<TContext extends AnyContext>(
  loop: LoopHandle<TContext>,
  context: TContext,
  options?: DoneOptions,
): Promise<Result<boolean>>;

export function trace(
  loop: LoopHandle,
  options?: TraceOptions,
): AsyncIterable<Trace>;
```

**为什么：**

- `create()` 同步返回 Result，因为它只做定义校验、handle 创建和 registry 绑定。
- `step()` 和 `done()` 返回 Promise，避免 runtime 对外部 evaluator 或 budget backend 做特殊处理。
- `trace()` 返回 `AsyncIterable`，同一个 API 可以读内存 trace，也可以流式读文件或数据库游标。

### 2.2 错误处理策略

策略：公共 runtime API 对预期错误返回 `Result<T, LoomError>`；loop 作者的异常会被 runtime 捕获并转成 `INTERNAL` trace + error。只有以下情况允许 throw：

- API 调用违反 TypeScript 无法表达的 invariant，例如 `undefined` loop handle。
- runtime 自身不可恢复损坏，例如 trace store 写入后无法确认一致性。
- 测试或开发模式下显式启用 `throwOnInvariantViolation`。

```typescript
export async function step(loop, context, options = {}) {
  assertLoopHandle(loop);
  const controller = composeAbort(options.signal, options.timeoutMs);

  try {
    const result = await loop.definition.step(context, makeStepRuntime(loop, controller));
    return result.ok
      ? await persistSuccessfulStep(result.value, options)
      : await persistFailedStep(loop, context, result.error, options);
  } catch (error) {
    return await persistThrownStep(loop, context, toLoomError(error), options);
  } finally {
    controller.cleanup();
  }
}
```

**为什么：**

- meta-loop 要分析失败，失败必须进入 trace，而不是作为未记录异常逃逸。
- `Result` 让 chain/nest/fork/meta 可以按各自策略 fail-fast 或 collect-errors。

### 2.3 异步模型

`step()` 是单次 observe-decide-act cycle。它可以内部调用多个异步 tool，但必须在返回前产生一个完整 `Trace`。如果需要长时间输出中间事件，通过 `TraceSink` streaming。

```typescript
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
  evaluate(context: AnyContext, criterion: SuccessCriterion, options?: DoneOptions): Promise<Result<boolean>>;
}

export interface ImplementationRegistry {
  get(ref: ImplementationRef): Result<StepFunction | DoneFunction>;
}

export interface RunMetrics {
  readonly steps: number;
  readonly startedAt: ISODateTime;
  readonly endedAt: ISODateTime;
  readonly durationMs: DurationMs;
  readonly traceCount: number;
  readonly outcome: TraceOutcome;
}

export interface RunResult<
  TContext extends AnyContext = AnyContext,
  TOutput extends JsonValue = JsonValue,
> {
  readonly context: TContext;
  readonly traces: readonly Trace[];
  readonly output?: TOutput;
  readonly metrics: RunMetrics;
}

export interface StepRuntime<TContext extends AnyContext = AnyContext> {
  readonly runId: RunId;
  readonly loopId: LoopId;
  readonly signal: AbortSignal;
  readonly registry: RuntimeRegistry;
  readonly traceSink: TraceSink;
  readonly now: () => ISODateTime;
  readonly applyPatch: (context: TContext, patch: ContextPatch<TContext>) => Result<TContext>;
  readonly callTool: (toolId: string, input: JsonValue, options?: ToolCallOptions) => Promise<Result<Observation>>;
  readonly runLoop: (loopId: LoopId, context: AnyContext, options?: RunOptions) => Promise<Result<RunResult>>;
}

export interface DoneRuntime {
  readonly signal: AbortSignal;
  readonly now: () => ISODateTime;
  readonly registry: RuntimeRegistry;
}
```

**为什么：**

- `StepRuntime` 集中提供 side-effect 能力，Context 本身保持数据化。
- loop 作者不直接写 trace store，而是通过 `traceSink` 发事件，由 runtime 决定采样和持久化。

### 2.4 Streaming traces

Trace streaming 分两层：

- `TraceEvent`：step 执行中的增量事件。
- `Trace`：step 结束后的完整记录。

```typescript
export type TraceEvent =
  | { readonly type: "step.started"; readonly traceId: TraceId; readonly at: ISODateTime; readonly contextId: ContextId }
  | { readonly type: "decision.recorded"; readonly traceId: TraceId; readonly decision: Decision; readonly at: ISODateTime }
  | { readonly type: "action.started"; readonly traceId: TraceId; readonly action: Action; readonly at: ISODateTime }
  | { readonly type: "observation.recorded"; readonly traceId: TraceId; readonly observation: Observation; readonly at: ISODateTime }
  | { readonly type: "child.started"; readonly traceId: TraceId; readonly childLoopId: LoopId; readonly childTraceId: TraceId; readonly at: ISODateTime }
  | { readonly type: "child.completed"; readonly traceId: TraceId; readonly childTraceId: TraceId; readonly outcome: TraceOutcome; readonly at: ISODateTime }
  | { readonly type: "step.completed"; readonly trace: Trace; readonly at: ISODateTime };

export interface TraceSink {
  emit(event: TraceEvent): Promise<Result<void>>;
}

export async function* stepStream<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(
  loop: LoopHandle<TContext, TObservation, TOutput>,
  context: TContext,
  options?: Omit<StepOptions, "traceSink">,
): AsyncGenerator<TraceEvent, Result<StepResult<TContext, TObservation, TOutput>>, void>;
```

**为什么：**

- `step()` 适合普通调用；`stepStream()` 适合 UI、debugger、remote worker。
- 中间事件不要求包含完整 context snapshot，避免大 step 造成内存放大。

### 2.5 超时和取消

统一使用 `AbortSignal`。`timeoutMs` 会被 runtime 转成内部 `AbortController`，并与用户传入 signal 组合。

```typescript
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
}
```

取消语义：

1. runtime 在 `step.started` 后收到 abort，必须生成 outcome 为 `cancelled` 的 Trace。
2. tool 和 child loop 必须接收同一个 signal 或其 child signal。
3. abort 不回滚已经完成的外部 side effect，但必须在 trace 中记录。
4. timeout 是 `TIMEOUT`，用户主动 abort 是 `ABORTED`。

**为什么：**

- `AbortController` 是 TypeScript/Node/Web 生态的标准取消机制。
- 取消进入 trace，meta-loop 才能区分预算不足、工具卡死和用户停止。

## 3. Context 五层的实现方案

### 3.1 存储方式

Context 的公共表示使用 plain object，内部可用索引加速查询。

```typescript
export interface ContextStore {
  put(context: AnyContext): Promise<Result<ContextId>>;
  get(id: ContextId): Promise<Result<AnyContext>>;
  applyPatch<TContext extends AnyContext>(
    context: TContext,
    patch: ContextPatch<TContext>,
  ): Result<TContext>;
}

export interface RuntimeIndexes {
  readonly knowledgeById: ReadonlyMap<string, KnowledgeItem>;
  readonly toolById: ReadonlyMap<string, ToolRef>;
  readonly loopById: ReadonlyMap<LoopId, LoopRef>;
}
```

存储选择：

- `identity`：plain object，创建后 deep freeze。
- `goal`：plain object，创建后 deep freeze。
- `state`：append-only arrays，patch 时复制数组尾部，旧数组不变。
- `knowledge`：不可变集合，父子可共享引用；runtime 建 `Map` 索引。
- `affordances`：引用集合，父子通过 allowlist 投影。

**为什么：**

- plain object 更适合 JSONL trace 和跨进程传输。
- `Map` 不直接进入 snapshot，避免序列化不稳定；索引可以从 snapshot 重建。

### 3.2 project(parent, child_goal)

`project()` 从 parent context 生成 child context。默认算法保证五层 scoping 与 `design.md` 一致。

```typescript
export interface ProjectOptions {
  readonly childIdentity: IdentityLayer;
  readonly childGoal: GoalLayer;
  readonly affordanceFilter?: (affordance: ToolRef | LoopRef | ResourceRef) => boolean;
  readonly knowledgeFilter?: (item: KnowledgeItem) => boolean;
  readonly includeStateSummary?: boolean;
  readonly metadata?: Metadata;
}

export interface ProjectResult<TChildContext extends AnyContext = AnyContext> {
  readonly childContext: TChildContext;
  readonly projectionTrace: Trace;
}

export function project<
  TParentContext extends AnyContext,
  TChildContext extends AnyContext = AnyContext,
>(
  parent: TParentContext,
  options: ProjectOptions,
): Result<ProjectResult<TChildContext>>;
```

伪代码：

```typescript
function project(parent, options) {
  validateChildGoal(options.childGoal);
  validateIdentity(options.childIdentity);

  const childKnowledge = selectKnowledge(parent.knowledge, options.knowledgeFilter);
  const childAffordances = selectAffordances(parent.affordances, options.affordanceFilter);

  const childState = options.includeStateSummary
    ? {
        observations: [summarizeParentState(parent.state)],
        decisions: [],
        pending: [],
      }
    : { observations: [], decisions: [], pending: [] };

  const childContext = freezeContext({
    id: newContextId(),
    runId: parent.runId,
    createdAt: now(),
    parentContextId: parent.id,
    identity: deepFreeze(options.childIdentity),
    goal: deepFreeze({
      ...options.childGoal,
      parentGoalId: parent.goal.metadata?.goalId as string | undefined,
    }),
    state: deepFreeze(childState),
    knowledge: makeReadOnlyKnowledge(childKnowledge),
    affordances: deepFreeze(childAffordances),
    metadata: options.metadata,
  });

  const projectionTrace = makeBoundaryTrace({
    kind: "project",
    parentContextId: parent.id,
    childContextId: childContext.id,
    selectedAffordanceIds: ids(childAffordances),
    selectedKnowledgeIds: ids(childKnowledge),
  });

  return ok({ childContext, projectionTrace });
}
```

**为什么：**

- child 获得自己的 `identity` 和 projected `goal`，避免父 loop 的角色污染子任务。
- child `state` 默认为空，保证子 loop 不依赖父 loop 的完整历史。
- `knowledge` 只读继承，`affordances` 只能缩小，不能扩大。

### 3.3 emit(child_ctx_out)

`emit()` 从 child final context 中提取可返回给 parent 的输出。它不直接修改 parent。

```typescript
export interface ChildOutput<TPayload extends JsonValue = JsonValue> {
  readonly childContextId: ContextId;
  readonly childLoopId: LoopId;
  readonly status: "completed" | "failed" | "cancelled" | "timeout";
  readonly payload?: TPayload;
  readonly observations: readonly Observation[];
  readonly decisions: readonly Decision[];
  readonly knowledgeCandidates: readonly KnowledgeItem[];
  readonly metrics: RunMetrics;
  readonly traceRootId?: TraceId;
  readonly error?: LoomError;
  readonly metadata?: Metadata;
}

export interface EmitOptions<TPayload extends JsonValue = JsonValue> {
  readonly payloadSelector?: (context: AnyContext) => TPayload;
  readonly knowledgeSelector?: (item: KnowledgeItem) => boolean;
  readonly observationSelector?: (observation: Observation) => boolean;
}

export function emit<TPayload extends JsonValue = JsonValue>(
  childContext: AnyContext,
  childLoop: LoopHandle,
  status: ChildOutput["status"],
  options?: EmitOptions<TPayload>,
): Result<ChildOutput<TPayload>>;
```

伪代码：

```typescript
function emit(childContext, childLoop, status, options = {}) {
  const observations = childContext.state.observations.filter(
    options.observationSelector ?? defaultObservationSelector,
  );

  const knowledgeCandidates = allKnowledge(childContext.knowledge).filter(
    options.knowledgeSelector ?? defaultKnowledgeSelector,
  );

  const output = {
    childContextId: childContext.id,
    childLoopId: childLoop.id,
    status,
    payload: options.payloadSelector?.(childContext),
    observations,
    decisions: childContext.state.decisions,
    knowledgeCandidates,
    metrics: computeRunMetrics(childLoop.id, childContext.runId),
    traceRootId: findTraceRoot(childLoop.id, childContext.runId),
  };

  return ok(deepFreeze(output));
}
```

**为什么：**

- emit 是 scope widening 前的 explicit extraction point。
- parent 可以审查 `knowledgeCandidates`，不会被 child 自动污染长期 knowledge。

### 3.4 merge(parent_ctx, child_output)

`merge()` 接收 parent context 和 child output，产生新的 parent context。默认 merge 只改 parent state；knowledge 需要 policy 允许。

```typescript
export interface MergePolicy {
  readonly acceptKnowledge?: (item: KnowledgeItem, parent: AnyContext) => boolean;
  readonly onConflict: "reject" | "prefer-parent" | "prefer-child" | "append-version";
  readonly appendChildSummaryObservation: boolean;
}

export function merge<TParentContext extends AnyContext>(
  parent: TParentContext,
  output: ChildOutput,
  policy?: MergePolicy,
): Result<TParentContext>;
```

伪代码：

```typescript
function merge(parent, output, policy = defaultMergePolicy) {
  const conflict = detectMergeConflict(parent, output);
  if (conflict && policy.onConflict === "reject") {
    return err({
      code: "MERGE_CONFLICT",
      message: conflict.message,
      retryable: false,
      cause: conflict,
    });
  }

  const summaryObservation = policy.appendChildSummaryObservation
    ? makeChildSummaryObservation(output)
    : undefined;

  const acceptedKnowledge = output.knowledgeCandidates.filter((item) =>
    policy.acceptKnowledge?.(item, parent) ?? false,
  );

  const patch = {
    baseContextId: parent.id,
    reason: `merge child ${output.childLoopId}`,
    operations: [
      ...(summaryObservation ? [{ op: "appendObservation", value: summaryObservation }] : []),
      ...output.observations.map((value) => ({ op: "appendObservation", value })),
      ...output.decisions.map((value) => ({ op: "appendDecision", value })),
      ...acceptedKnowledge.map((value) => ({ op: "addKnowledge", value })),
    ],
  };

  return applyContextPatch(parent, patch);
}
```

**为什么：**

- parent 的 `identity`、`goal`、`affordances` 不被 child output 覆盖，符合边界隔离。
- knowledge 合并是 policy-driven，因为 child 发现可能是噪声、局部事实或低置信度记忆。

### 3.5 深拷贝、浅拷贝与不可变策略

默认策略是不可变结构加结构共享，而不是每次完整 deep clone。

| Layer | 策略 | 原因 |
|---|---|---|
| identity | 创建时 deep freeze；project 时新建 child identity | 身份是边界定义，不应被运行时修改 |
| goal | 创建时 deep freeze；子 goal 新建 | goal 决定 done 语义，必须稳定 |
| state | append-only structural sharing | step 频繁更新，完整 deep clone 成本高 |
| knowledge | read-only 共享引用，更新产生新 version | knowledge 可大，父子共享节省内存 |
| affordances | 引用集合过滤后 freeze | 实际 tool/loop 在 registry 中，不在 Context 中复制 |

运行时实现：

```typescript
export function freezeContext<TContext extends AnyContext>(context: TContext): TContext {
  return deepFreeze({
    ...context,
    identity: deepFreeze(context.identity),
    goal: deepFreeze(context.goal),
    state: deepFreeze(context.state),
    knowledge: deepFreeze(context.knowledge),
    affordances: deepFreeze(context.affordances),
  });
}
```

**为什么：**

- trace snapshot 需要历史一致性；不可变比约定式“不修改”可靠。
- structuredClone 只在跨 worker、跨进程、落盘前使用，日常 step 使用结构共享。

### 3.6 knowledge read-only 继承

生产实现不依赖 Proxy；通过类型和 capability design 保证只读。开发模式可用 Proxy 捕获非法写入。

```typescript
export interface KnowledgeQuery {
  readonly text?: string;
  readonly kind?: readonly KnowledgeItem["kind"][];
  readonly minConfidence?: number;
  readonly sourceTraceId?: TraceId;
  readonly limit?: number;
  readonly tags?: readonly string[];
}

export interface KnowledgeView {
  get(id: string): KnowledgeItem | undefined;
  search(query: KnowledgeQuery): readonly KnowledgeItem[];
  all(): readonly KnowledgeItem[];
}

export interface KnowledgeWriter {
  propose(item: KnowledgeItem): Result<KnowledgeItem>;
}

export interface KnowledgeAccess {
  readonly read: KnowledgeView;
  readonly write?: KnowledgeWriter;
}
```

规则：

- child runtime 只拿到 `KnowledgeAccess.read`。
- child 产生新 knowledge 时写入自己的 `state` 或 `knowledgeCandidates`，不能写 parent knowledge。
- parent merge policy 决定是否接受候选 knowledge。
- dev mode 可以包一层 Proxy，对写入抛 invariant error。

**为什么：**

- Proxy 有运行时成本，也容易影响序列化和调试。
- 只读接口加 frozen data 足够覆盖生产安全性；Proxy 适合作为测试辅助。

## 4. 四种组合模式的具体实现

### 4.1 chain

签名：

```typescript
export interface ChainOptions {
  readonly id?: LoopId;
  readonly identity?: IdentityLayer;
  readonly goal?: GoalLayer;
  readonly errorMode?: "fail-fast" | "continue";
  readonly traceMode?: "concatenate" | "nested";
  readonly metadata?: Metadata;
}

export function chain<
  TContext extends AnyContext,
  TOutput extends JsonValue = JsonValue,
>(
  loops: readonly LoopHandle<TContext, Observation, TOutput>[],
  options?: ChainOptions,
): Result<LoopHandle<TContext, Observation, TOutput>>;
```

执行流程：

```typescript
async function chainStep(context, runtime) {
  let current = context;
  const childTraces = [];
  const outputs = [];

  for (const child of loops) {
    const isDone = await done(child, current, { signal: runtime.signal });
    if (!isDone.ok) return err(isDone.error);

    if (!isDone.value) {
      const result = await step(child, current, {
        signal: runtime.signal,
        traceSink: runtime.traceSink,
      });

      if (!result.ok) {
        if (options.errorMode === "continue") {
          childTraces.push(makeFailureTrace(child, current, result.error));
          continue;
        }
        return err(result.error);
      }

      current = result.value.context;
      childTraces.push(result.value.trace);
      if (result.value.output !== undefined) outputs.push(result.value.output);
    }
  }

  return ok({
    context: current,
    trace: makeCompositeTrace("chain", context, current, childTraces),
    output: outputs.at(-1),
  });
}
```

错误传播：

- 默认 `fail-fast`，因为 chain 的后续 loop 依赖前序 context。
- 可选 `continue` 只适合 best-effort pipeline，例如“尽量运行所有静态检查”。

**为什么：**

- chain 语义接近 Unix pipe；上游失败时下游输入通常不可信。
- trace 可以 concatenate，但 composite trace 仍保留 child trace ids 便于树查询。

### 4.2 nest

签名：

```typescript
export interface NestOptions<TPayload extends JsonValue = JsonValue> {
  readonly id?: LoopId;
  readonly when: (parentContext: AnyContext) => MaybePromise<Result<boolean>>;
  readonly project: (parentContext: AnyContext) => Result<ProjectResult>;
  readonly emit: (childContext: AnyContext, childLoop: LoopHandle, status: ChildOutput["status"]) => Result<ChildOutput<TPayload>>;
  readonly merge: (parentContext: AnyContext, childOutput: ChildOutput<TPayload>) => Result<AnyContext>;
  readonly childRunOptions?: RunOptions;
  readonly errorMode?: "propagate" | "record-and-continue";
  readonly metadata?: Metadata;
}

export function nest(
  outer: LoopHandle,
  inner: LoopHandle,
  options: NestOptions,
): Result<LoopHandle>;
```

执行流程：

```typescript
async function nestedStep(parentContext, runtime) {
  const outerResult = await step(outer, parentContext, {
    signal: runtime.signal,
    traceSink: runtime.traceSink,
  });
  if (!outerResult.ok) return outerResult;

  let currentParent = outerResult.value.context;
  const shouldInvoke = await options.when(currentParent);
  if (!shouldInvoke.ok) return err(shouldInvoke.error);
  if (!shouldInvoke.value) return outerResult;

  const projected = options.project(currentParent);
  if (!projected.ok) return err(projected.error);

  runtime.traceSink.emit({
    type: "child.started",
    traceId: outerResult.value.trace.id,
    childLoopId: inner.id,
    childTraceId: projected.value.projectionTrace.id,
    at: runtime.now(),
  });

  const childRun = await run(inner, projected.value.childContext, {
    ...options.childRunOptions,
    signal: runtime.signal,
    traceSink: runtime.traceSink,
  });

  const status = childRun.ok ? "completed" : errorToChildStatus(childRun.error);
  const childContext = childRun.ok ? childRun.value.context : projected.value.childContext;
  const emitted = options.emit(childContext, inner, status);
  if (!emitted.ok) return err(emitted.error);

  if (!childRun.ok && options.errorMode !== "record-and-continue") {
    return err(childRun.error);
  }

  const merged = options.merge(currentParent, emitted.value);
  if (!merged.ok) return err(merged.error);

  return ok({
    context: merged.value,
    trace: makeCompositeTrace("nest", parentContext, merged.value, [
      outerResult.value.trace,
      projected.value.projectionTrace,
      ...(childRun.ok ? childRun.value.traces : []),
    ]),
    output: emitted.value.payload,
  });
}
```

错误传播：

- 默认 `propagate`，child failure 使 parent step failure。
- `record-and-continue` 适合辅助子 loop，例如 linter 或 optional reviewer。

**为什么：**

- nest 是 loop boundary，必须 trace project 和 emit/merge。
- parent 通过 `when/project/emit/merge` 控制边界，不让 child 任意读取或写回 parent。

### 4.3 fork

签名：

```typescript
export interface ForkOptions<TSlice extends JsonValue = JsonValue, TMerged extends JsonValue = JsonValue> {
  readonly id?: LoopId;
  readonly split: (context: AnyContext) => Result<readonly TSlice[]>;
  readonly projectSlice: (context: AnyContext, slice: TSlice, index: number) => Result<ProjectResult>;
  readonly mergeOutputs: (context: AnyContext, outputs: readonly ChildOutput[]) => Result<{ readonly context: AnyContext; readonly output?: TMerged }>;
  readonly concurrency?: number;
  readonly errorMode?: "collect-errors" | "fail-fast" | "quorum";
  readonly quorum?: number;
  readonly workerTimeoutMs?: number;
  readonly metadata?: Metadata;
}

export function fork<TSlice extends JsonValue = JsonValue, TMerged extends JsonValue = JsonValue>(
  worker: LoopHandle,
  options: ForkOptions<TSlice, TMerged>,
): Result<LoopHandle<AnyContext, Observation, TMerged>>;
```

并发模型：

- 默认用 in-process promise worker pool。
- `concurrency` 默认是 `min(sliceCount, runtime.defaultConcurrency)`。
- CPU-bound 或隔离要求高的 worker 后续可以换成 Worker Threads adapter，但 API 不变。

执行流程：

```typescript
async function forkStep(context, runtime) {
  const slices = options.split(context);
  if (!slices.ok) return err(slices.error);

  const pool = createPromisePool(options.concurrency ?? defaultConcurrency());
  const outputs = [];
  const errors = [];

  await pool.run(slices.value.map((slice, index) => async () => {
    if (runtime.signal.aborted) return;

    const projected = options.projectSlice(context, slice, index);
    if (!projected.ok) {
      errors.push(projected.error);
      return;
    }

    const child = await run(worker, projected.value.childContext, {
      signal: runtime.signal,
      timeoutMs: options.workerTimeoutMs,
      traceSink: tagTraceSink(runtime.traceSink, { forkIndex: index }),
    });

    if (!child.ok) {
      errors.push(child.error);
      if (options.errorMode === "fail-fast") runtime.signal.throwIfAborted();
      return;
    }

    const emitted = emit(child.value.context, worker, "completed", {
      payloadSelector: defaultForkPayloadSelector,
    });
    emitted.ok ? outputs.push(emitted.value) : errors.push(emitted.error);
  }));

  if (options.errorMode === "quorum" && outputs.length < (options.quorum ?? slices.value.length)) {
    return err(makeQuorumError(outputs, errors));
  }

  if (options.errorMode === "fail-fast" && errors.length > 0) {
    return err(errors[0]);
  }

  const merged = options.mergeOutputs(context, outputs);
  if (!merged.ok) return err(merged.error);

  return ok({
    context: merged.value.context,
    trace: makeForkTrace(context, merged.value.context, outputs, errors),
    output: merged.value.output,
  });
}
```

错误传播：

- 默认 `collect-errors`，因为 fork 天然支持部分成功。
- `fail-fast` 用于所有 slice 都必须成功的任务。
- `quorum` 用于 ensemble 或投票任务。

**为什么：**

- Promise pool 比裸 `Promise.all` 更可控，可以限制 tool/API 并发。
- sibling contexts 隔离执行，只通过 `mergeOutputs` 汇总，避免竞态写 parent context。

### 4.4 meta

签名：

```typescript
export interface MetaOptions {
  readonly id?: LoopId;
  readonly mutationPolicy: MutationPolicy;
  readonly evaluator: EvolutionEvaluator;
  readonly maxRounds?: number;
  readonly applyAt: "round-boundary" | "step-boundary";
  readonly rollbackOnFailure: boolean;
  readonly metadata?: Metadata;
}

export function meta(
  inner: LoopHandle,
  evolver: LoopHandle<AnyContext, Observation, MutationBundle>,
  options: MetaOptions,
): Result<LoopHandle>;
```

mutation 数据结构：

```typescript
export interface MutationBundle {
  readonly id: string;
  readonly targetLoopId: LoopId;
  readonly baseVersion: LoopVersion;
  readonly mutations: readonly Mutation[];
  readonly rationale: string;
  readonly expectedImpact: readonly ExpectedImpact[];
  readonly risk: "low" | "medium" | "high";
  readonly createdFromTraceIds: readonly TraceId[];
  readonly metadata?: Metadata;
}

export type Mutation =
  | ContextMutation
  | LoopMutation
  | StructureMutation;

export interface ContextMutation {
  readonly level: 1;
  readonly kind: "context";
  readonly patch: ContextPatch;
}

export interface LoopMutation {
  readonly level: 2;
  readonly kind: "loop";
  readonly target: "step" | "done" | "identity" | "goal";
  readonly replacementRef?: ImplementationRef;
  readonly patch?: PatchOperation;
}

export interface StructureMutation {
  readonly level: 3;
  readonly kind: "structure";
  readonly operation: "insert-loop" | "remove-loop" | "replace-loop" | "change-composition";
  readonly graphPatch: CompositionGraphPatch;
}

export interface CompositionGraphPatch {
  readonly insertAfter?: string;
  readonly insertBefore?: string;
  readonly removeNodeId?: string;
  readonly replaceNodeId?: string;
  readonly loopRef?: string;
  readonly composition: "chain" | "nest" | "fork" | "meta";
  readonly metadata?: Metadata;
}

export interface ExpectedImpact {
  readonly metric: string;
  readonly direction: "increase" | "decrease";
  readonly rationale: string;
}
```

应用机制：

```typescript
async function metaStep(context, runtime) {
  const active = loopRegistry.getActive(inner.id);
  const before = await run(active, context, { signal: runtime.signal, traceSink: runtime.traceSink });
  if (!before.ok) return err(before.error);

  const evolutionContext = buildEvolutionContext(before.value.traces, active);
  const mutationResult = await run(evolver, evolutionContext, {
    signal: runtime.signal,
    traceSink: runtime.traceSink,
  });
  if (!mutationResult.ok) return err(mutationResult.error);

  const bundle = mutationResult.value.output;
  const validation = await validateMutationBundle(bundle, active, options.mutationPolicy);
  if (!validation.ok) return err(validation.error);

  const transaction = await loopRegistry.beginMutation(active.id, active.version);
  const candidate = await transaction.apply(bundle);
  if (!candidate.ok) {
    await transaction.rollback();
    return err(candidate.error);
  }

  const evaluation = await options.evaluator.compare(active, candidate.value, before.value.traces);
  if (!evaluation.ok || !evaluation.value.accepted) {
    await transaction.rollback();
    return evaluation.ok
      ? ok(makeRejectedMutationStep(context, before.value, evaluation.value))
      : err(evaluation.error);
  }

  await transaction.commit();
  return ok({
    context: before.value.context,
    trace: makeMetaTrace(active, candidate.value, before.value.traces, mutationResult.value.traces),
    output: { activeVersion: candidate.value.version },
  });
}
```

安全规则：

- 不原地修改正在执行的 loop definition。
- mutation 生成新 `LoopVersion`，通过 registry 在 `round-boundary` 或 `step-boundary` 热切换。
- `baseVersion` 必须匹配，否则拒绝 mutation，防止并发 evolution 覆盖。
- Level 2 的 step/done replacement 只能引用已注册实现，不能从 trace 中直接执行任意字符串代码。

**为什么：**

- versioned immutable loop 是 meta 安全性的核心。
- mutation 作为事务应用，失败时能回滚到上一版本。
- 禁止任意代码字符串可以降低演化引擎被 trace 注入攻击的风险。

## 5. Trace 系统的详细设计

### 5.1 Trace 类型

Trace 是 step 的 first-class output。组合 trace 通过 `parentTraceId/rootTraceId/children` 构成树。

```typescript
export type TraceOutcome = "pass" | "fail" | "cancelled" | "timeout" | "skipped";

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
  readonly kind: "step" | "project" | "emit" | "merge" | "chain" | "nest" | "fork" | "meta";

  readonly inputContextId: ContextId;
  readonly outputContextId?: ContextId;
  readonly inputSnapshot?: TraceSnapshot<TContext>;
  readonly outputSnapshot?: TraceSnapshot<TContext>;
  readonly contextDelta?: ContextPatch<TContext>;

  readonly action: Action;
  readonly result?: TObservation;
  readonly reasoning: string;
  readonly alternatives: readonly Action[];
  readonly confidence: number;

  readonly outcome: TraceOutcome;
  readonly gap?: string;
  readonly surprise?: string;
  readonly error?: LoomError;
  readonly startedAt: ISODateTime;
  readonly endedAt: ISODateTime;
  readonly durationMs: DurationMs;

  readonly children: readonly TraceId[];
  readonly tags?: readonly string[];
  readonly metadata?: Metadata;
}

export interface TraceSnapshot<TContext extends AnyContext = AnyContext> {
  readonly contextId: ContextId;
  readonly encoding: "inline" | "content-ref";
  readonly value?: Immutable<TContext>;
  readonly contentRef?: string;
  readonly hash: string;
}
```

**为什么：**

- 同时支持 inline snapshot 和 content-ref，MVP 可内联，规模化后可去重。
- `contextDelta` 让大 trace 不必每步存完整 context。
- `gap` 和 `surprise` 是 evolution 的关键字段，保留为顶层字段。

### 5.2 Trace 存储后端

MVP 使用内存优先，但接口先抽象出可替换 store。

```typescript
export interface TraceStore {
  append(trace: Trace): Promise<Result<void>>;
  appendEvent(event: TraceEvent): Promise<Result<void>>;
  get(id: TraceId): Promise<Result<Trace>>;
  query(query: TraceQuery): AsyncIterable<Trace>;
  children(id: TraceId): AsyncIterable<Trace>;
  close(): Promise<Result<void>>;
}

export interface TraceQuery {
  readonly runId?: RunId;
  readonly loopId?: LoopId;
  readonly rootTraceId?: TraceId;
  readonly parentTraceId?: TraceId;
  readonly outcome?: readonly TraceOutcome[];
  readonly tags?: readonly string[];
  readonly startedAfter?: ISODateTime;
  readonly startedBefore?: ISODateTime;
  readonly text?: string;
  readonly limit?: number;
}
```

实现顺序：

1. `InMemoryTraceStore`：数组 + Map 索引，用于测试和短任务。
2. `JsonlTraceStore`：append-only JSONL 文件，用于本地持久化。
3. `SqliteTraceStore`：trace metadata 入表，snapshot/blob 入对象表，用于查询。

**为什么：**

- 内存优先能快速实现核心 loop 和组合。
- 接口先稳定，后续 persistent store 不改变 loop API。
- JSONL 是最简单的可调试持久化格式，SQLite 适合后续查询和索引。

### 5.3 Trace 树构建与查询 API

Trace 树不需要在写入时保存完整树对象；通过 parent/root id 构建。

```typescript
export interface TraceNode {
  readonly trace: Trace;
  readonly children: readonly TraceNode[];
}

export interface TraceReader {
  query(options?: TraceOptions): AsyncIterable<Trace>;
  get(id: TraceId): Promise<Result<Trace>>;
  tree(rootTraceId: TraceId, options?: TraceTreeOptions): Promise<Result<TraceNode>>;
  path(traceId: TraceId): Promise<Result<readonly Trace[]>>;
  summarize(query: TraceQuery): Promise<Result<TraceSummary>>;
}

export interface TraceTreeOptions {
  readonly maxDepth?: number;
  readonly includeSnapshots?: boolean;
  readonly includeEvents?: boolean;
}

export interface TraceSummary {
  readonly count: number;
  readonly byOutcome: Readonly<Record<TraceOutcome, number>>;
  readonly averageDurationMs: number;
  readonly gaps: readonly string[];
  readonly surprises: readonly string[];
  readonly errors: readonly LoomError[];
}
```

构建算法：

```typescript
async function buildTraceTree(store, rootTraceId, options) {
  const root = await store.get(rootTraceId);
  if (!root.ok) return root;

  async function build(trace, depth) {
    if (options.maxDepth !== undefined && depth >= options.maxDepth) {
      return { trace: stripIfNeeded(trace, options), children: [] };
    }

    const children = [];
    for await (const child of store.children(trace.id)) {
      children.push(await build(child, depth + 1));
    }

    return { trace: stripIfNeeded(trace, options), children };
  }

  return ok(await build(root.value, 0));
}
```

**为什么：**

- parent/root id 支持流式写入；不需要在父 trace 完成前知道全部 children。
- tree API 在读取时构建，便于按 depth 和 snapshot policy 控制成本。

### 5.4 大 trace 序列化与反序列化

大 trace 使用分块 JSONL：

```typescript
export interface SerializedTraceRecord {
  readonly type: "trace" | "event" | "snapshot" | "index";
  readonly id: string;
  readonly runId: RunId;
  readonly payload: JsonValue;
  readonly hash: string;
}
```

策略：

- 每行一个 record，append-only。
- snapshot 大于阈值时写成 `snapshot` record，trace 只保存 `contentRef`。
- 每 N 条 trace 写一个 `index` record，记录 offset、rootTraceId、loopId、outcome。
- 反序列化时先读 index，再按 offset lazy load trace 和 snapshot。

**为什么：**

- JSONL 可在进程崩溃时最大限度保留已写入记录。
- snapshot 去重和 lazy load 能避免一次性加载完整历史。

### 5.5 Trace 采样策略

不是每个 step 都需要完整 snapshot。默认 policy：

```typescript
export interface TraceSamplePolicy {
  readonly fullSnapshotOn: readonly ("run-start" | "run-end" | "failure" | "boundary" | "every-n-steps")[];
  readonly everyNSteps?: number;
  readonly includeDelta: boolean;
  readonly includeReasoning: boolean;
  readonly maxInlineSnapshotBytes: number;
}

export const defaultTraceSamplePolicy: TraceSamplePolicy = {
  fullSnapshotOn: ["run-start", "run-end", "failure", "boundary"],
  includeDelta: true,
  includeReasoning: true,
  maxInlineSnapshotBytes: 64_000,
};
```

**为什么：**

- failure 和 boundary 是 debug/evolution 价值最高的位置。
- delta 足以回放普通成功 step，完整 snapshot 只在关键点保留。

### 5.6 Trace 压缩和归档

归档流程：

1. close run 后计算 trace DAG 引用关系。
2. content-address snapshot 去重。
3. JSONL 分块 gzip 或 zstd 压缩。
4. 生成 manifest：runId、root traces、loop versions、hash、schema version。
5. 原始 store 保留近期热数据，冷数据移动到 archive。

```typescript
export interface TraceArchiveManifest {
  readonly schemaVersion: string;
  readonly runId: RunId;
  readonly createdAt: ISODateTime;
  readonly traceCount: number;
  readonly rootTraceIds: readonly TraceId[];
  readonly loopVersions: Readonly<Record<string, string>>;
  readonly chunks: readonly ArchiveChunk[];
}

export interface ArchiveChunk {
  readonly path: string;
  readonly compression: "gzip" | "zstd" | "none";
  readonly hash: string;
  readonly recordCount: number;
}
```

**为什么：**

- trace 是 evolution 语料，不能简单丢弃。
- 冷热分层让日常查询快速，同时保留长期学习材料。

## 6. 演化引擎的详细设计

### 6.1 Level 1/2/3 触发条件

演化遵循“先尝试最便宜层级”的原则。

| Level | 触发条件 | 典型 trace 信号 | 例子 |
|---|---|---|---|
| Level 1 Context | 信息缺失、约束不清、tool 描述误导 | `gap` 非空、低 confidence、同类查询反复补问 | 添加 heuristic、收紧 identity constraint |
| Level 2 Loop | step 逻辑假设错误、done 过早/过晚、tool wrapper bug | `surprise` 非空、同一步骤反复失败、retryable=false | 替换 step implementation ref、调整 done |
| Level 3 Structure | 边界错误、串行过慢、职责混杂、多 loop 重复失败 | repeated timeout、跨 loop 相同错误、trace tree 过深或过宽 | chain 改 fork、插入 validator loop |

具体触发器：

```typescript
export interface EvolutionTrigger {
  readonly id: string;
  readonly level: 1 | 2 | 3;
  readonly predicate: (summary: TraceSummary, traces: readonly Trace[]) => boolean;
  readonly priority: number;
  readonly description: string;
}
```

**为什么：**

- 表驱动 trigger 便于测试和调参。
- Level 是建议，不是强制；最终由 strategy 决策树选择。

### 6.2 Mutation 表示

Mutation 必须可以被审查、回滚、关联 trace 和评估。

```typescript
export interface MutationPolicy {
  readonly allowedLevels: readonly (1 | 2 | 3)[];
  readonly requireEvidenceTrace: boolean;
  readonly maxRisk: "low" | "medium" | "high";
  readonly allowCodeReplacement: boolean;
  readonly allowStructureChange: boolean;
}

export interface MutationTransaction {
  readonly id: string;
  readonly targetLoopId: LoopId;
  readonly baseVersion: LoopVersion;
  apply(bundle: MutationBundle): Promise<Result<LoopHandle>>;
  commit(): Promise<Result<LoopVersion>>;
  rollback(): Promise<Result<void>>;
}
```

Level 1 示例：

```typescript
const mutation: ContextMutation = {
  level: 1,
  kind: "context",
  patch: {
    baseContextId,
    reason: "Trace gaps show permission errors lacked ownership heuristic",
    operations: [
      {
        op: "addKnowledge",
        value: {
          id: "heuristic.permission-ownership-first",
          kind: "heuristic",
          content: {
            when: "tool returns EACCES or permission denied",
            do: "inspect file ownership and permission bits before retrying",
          },
          confidence: 0.82,
          createdAt: now(),
        },
      },
    ],
  },
};
```

Level 2 示例：

```typescript
const mutation: LoopMutation = {
  level: 2,
  kind: "loop",
  target: "done",
  replacementRef: "impl:done:criteria-and-budget-v2" as ImplementationRef,
};
```

Level 3 示例：

```typescript
const mutation: StructureMutation = {
  level: 3,
  kind: "structure",
  operation: "insert-loop",
  graphPatch: {
    insertAfter: "loop:execute",
    loopRef: "loop:verify",
    composition: "chain",
  },
};
```

**为什么：**

- context mutation 是 patch，直接可回放。
- loop mutation 使用 implementation ref，避免动态执行不可信代码。
- structure mutation 使用 composition graph patch，保证结构变化可验证。

### 6.3 回滚机制

回滚基于版本化 registry。

```typescript
export interface LoopRegistry {
  getActive(loopId: LoopId): Result<LoopHandle>;
  getVersion(loopId: LoopId, version: LoopVersion): Result<LoopHandle>;
  beginMutation(loopId: LoopId, baseVersion: LoopVersion): Promise<Result<MutationTransaction>>;
}
```

事务流程：

```typescript
async function applyEvolution(bundle, registry, evaluator) {
  const active = registry.getActive(bundle.targetLoopId);
  if (!active.ok) return active;
  if (active.value.version !== bundle.baseVersion) return err(versionMismatch());

  const tx = await registry.beginMutation(active.value.id, active.value.version);
  if (!tx.ok) return err(tx.error);

  const candidate = await tx.value.apply(bundle);
  if (!candidate.ok) {
    await tx.value.rollback();
    return err(candidate.error);
  }

  const evaluation = await evaluator.evaluateCandidate(active.value, candidate.value);
  if (!evaluation.ok || !evaluation.value.accepted) {
    await tx.value.rollback();
    return evaluation.ok ? err(mutationRejected(evaluation.value)) : err(evaluation.error);
  }

  return await tx.value.commit();
}
```

回滚边界：

- Level 1：丢弃新 context version 或 knowledge candidate。
- Level 2：active loop version 指针回到旧版本。
- Level 3：composition graph version 指针回到旧版本。
- 外部 side effect 不做物理回滚，只通过 compensating loop 处理。

**为什么：**

- 对 framework state 做事务回滚是可控的。
- 对外部世界做强事务不现实，所以必须通过 trace 暴露 side effect 并由补偿策略处理。

### 6.4 演化策略决策树

```typescript
export interface EvolutionDecision {
  readonly level: 1 | 2 | 3;
  readonly reason: string;
  readonly evidenceTraceIds: readonly TraceId[];
  readonly proposedMutationKinds: readonly Mutation["kind"][];
}

export function decideEvolution(summary: TraceSummary, traces: readonly Trace[]): EvolutionDecision {
  const repeatedGaps = groupByText(traces.flatMap((trace) => trace.gap ? [trace.gap] : []));
  const repeatedSurprises = groupByText(traces.flatMap((trace) => trace.surprise ? [trace.surprise] : []));
  const structuralSignals = detectStructuralSignals(traces);

  if (hasHighConfidenceInformationGap(repeatedGaps)) {
    return {
      level: 1,
      reason: "Repeated gap indicates missing context, cheapest reversible fix applies",
      evidenceTraceIds: tracesWithGaps(traces),
      proposedMutationKinds: ["context"],
    };
  }

  if (hasStepLogicFailure(repeatedSurprises, summary.errors)) {
    return {
      level: 2,
      reason: "Repeated surprise indicates step or done logic assumption is wrong",
      evidenceTraceIds: tracesWithSurprises(traces),
      proposedMutationKinds: ["loop"],
    };
  }

  if (structuralSignals.timeoutRate > 0.3 || structuralSignals.crossLoopFailureCount >= 3) {
    return {
      level: 3,
      reason: "Failures span loop boundaries or budget shape, structure likely mismatched",
      evidenceTraceIds: structuralSignals.traceIds,
      proposedMutationKinds: ["structure"],
    };
  }

  return {
    level: 1,
    reason: "No strong signal; prefer low-risk context refinement",
    evidenceTraceIds: traces.map((trace) => trace.id),
    proposedMutationKinds: ["context"],
  };
}
```

**为什么：**

- 相同 evidence 下优先 Level 1，符合最小风险原则。
- Level 3 需要跨 trace 或跨 loop 信号，避免因单次失败重构系统。

### 6.5 评估函数

一次演化是否成功，不能只看“没有报错”。需要比较 mutation 前后的目标指标。

```typescript
export interface EvolutionEvaluation {
  readonly accepted: boolean;
  readonly scoreBefore: number;
  readonly scoreAfter: number;
  readonly delta: number;
  readonly confidence: number;
  readonly regressions: readonly Regression[];
  readonly rationale: string;
}

export interface EvolutionEvaluator {
  compare(
    before: LoopHandle,
    candidate: LoopHandle,
    evidence: readonly Trace[],
  ): Promise<Result<EvolutionEvaluation>>;
}

export interface Regression {
  readonly metric: string;
  readonly before: number;
  readonly after: number;
  readonly severity: "low" | "medium" | "high";
}
```

默认评分：

```typescript
function scoreRun(summary: TraceSummary, goal: GoalLayer): number {
  const passRate = ratio(summary.byOutcome.pass, summary.count);
  const failurePenalty = ratio(summary.byOutcome.fail, summary.count) * 0.4;
  const timeoutPenalty = ratio(summary.byOutcome.timeout, summary.count) * 0.3;
  const durationPenalty = normalizeDuration(summary.averageDurationMs, goal.budget.maxDurationMs) * 0.2;
  const gapPenalty = Math.min(summary.gaps.length * 0.03, 0.2);

  return clamp(passRate - failurePenalty - timeoutPenalty - durationPenalty - gapPenalty, 0, 1);
}
```

接受条件：

- `scoreAfter >= scoreBefore + minDelta`。
- 没有 high severity regression。
- mutation risk 不超过 policy。
- evidence trace 覆盖目标失败类型。

**为什么：**

- 演化可能改善局部指标但引入回归，必须显式检查。
- score 是策略接口，不是硬编码真理；业务可替换 evaluator。

## 7. 模块架构和文件结构

目标目录结构：

```text
loom/
  src/
    core/
      ids.ts
      immutable.ts
      result.ts
      loop.ts
      context.ts
      trace.ts
      errors.ts
      index.ts
    composition/
      chain.ts
      nest.ts
      fork.ts
      meta.ts
      graph.ts
      index.ts
    evolution/
      mutations.ts
      triggers.ts
      strategy.ts
      evaluator.ts
      engine.ts
      registry.ts
      index.ts
    runtime/
      create.ts
      step.ts
      done.ts
      run.ts
      scheduler.ts
      cancellation.ts
      registry.ts
      index.ts
    observability/
      trace-store.ts
      in-memory-trace-store.ts
      jsonl-trace-store.ts
      trace-reader.ts
      sampling.ts
      archive.ts
      index.ts
    examples/
      minimal-loop.ts
      chain-pipeline.ts
      nested-tool.ts
      fork-reviewers.ts
      level1-evolution.ts
  tests/
    core/
    composition/
    evolution/
    runtime/
    observability/
  docs/
```

模块职责：

| Module | 职责 | 主要导出 |
|---|---|---|
| `core` | 类型、Result、错误、Context/Trace/Loop 抽象 | `MinimalLoopDefinition`, `Context`, `Trace`, `Result`, `LoomError` |
| `composition` | 四种组合模式和 composition graph | `chain`, `nest`, `fork`, `meta`, `CompositionGraph` |
| `evolution` | mutation、trigger、strategy、evaluator、registry transaction | `MutationBundle`, `EvolutionEngine`, `decideEvolution` |
| `runtime` | create/step/done/run、调度、取消、tool/loop registry | `create`, `step`, `done`, `run`, `RuntimeRegistry` |
| `observability` | trace 收集、存储、查询、采样、归档 | `TraceStore`, `TraceReader`, `InMemoryTraceStore`, `JsonlTraceStore` |
| `examples` | 可运行示例，不被核心依赖 | example loops |

依赖方向：

```text
core
  ↑
observability
  ↑
runtime
  ↑
composition
  ↑
evolution

examples depend on all public modules.
tests may import internal modules.
```

约束：

- `core` 不依赖任何其他 Loom module。
- `observability` 依赖 `core`，不能依赖 `runtime`。
- `runtime` 依赖 `core` 和 `observability`。
- `composition` 依赖 `runtime`，通过公共 API 调用 child loops。
- `evolution` 可依赖 `composition`，但 mutation 数据结构放在 `evolution/mutations.ts`，避免 runtime 反向依赖。

**为什么：**

- 类型和数据模型必须稳定在最底层。
- trace store 不应知道 scheduler 细节，否则持久化会耦合执行模型。
- evolution 是最高层能力，可以组合所有底层能力。

## 8. 关键设计决策和 Trade-off 分析

### 8.1 语言选择：TypeScript vs Python

决策：推荐 TypeScript。

为什么：

- Loom 是框架，核心价值在统一接口和组合边界；TypeScript 的结构类型和泛型能在编译期约束 Context、Trace、Mutation。
- Node/Web 生态原生支持 `AbortController`, `AsyncIterable`, stream 等异步基础设施。
- Agent tool calls、UI trace viewer、remote runtime 都更容易共享 TypeScript 类型。

Trade-off：

- Python 的 AI/ML 生态更强，实验速度快。
- TypeScript 的不可变和 JSON schema 边界更适合框架层；Python adapter 可以作为后续 runtime bridge。

### 8.2 step() 同步 vs 异步

决策：`step()` 固定 async，返回 `Promise<Result<StepResult>>`。

为什么：

- tool calls、sub-loop、文件系统、网络、模型调用必然异步。
- 同步 step 可以自然写成 `async` 函数返回 resolved Promise，不需要额外 API。
- trace streaming 和取消都需要 async control flow。

Trade-off：

- 单纯 CPU 内存 loop 会多一层 Promise 成本。
- 这个成本相比 agent runtime 的 IO 成本可以忽略，统一模型更重要。

### 8.3 Context 深拷贝 vs 不可变数据结构

决策：推荐不可变数据结构 + 结构共享，必要时 snapshot serialization。

为什么：

- trace snapshot 要历史一致，原地修改会破坏回放。
- 完整 deep clone 每步成本过高，knowledge 和 affordances 可能很大。
- `ContextPatch` 能表达变化，便于 trace delta 和回滚。

Trade-off：

- 实现比 mutable object 复杂，需要 freeze、patch、schema validation。
- 但复杂度集中在 `core/context.ts`，换来整个系统的可追踪性。

### 8.4 组合模式错误处理：fail-fast vs collect-errors

决策：默认按组合语义选择：

- chain：默认 `fail-fast`。
- nest：默认 `propagate`。
- fork：默认 `collect-errors`。
- meta：mutation apply 默认 fail-safe rollback。

为什么：

- chain 后续依赖前序输出，fail-fast 最符合语义。
- nest 的 child 通常是 parent step 的一部分，默认失败应影响 parent。
- fork 的 sibling 独立，部分成功有价值，应收集错误后交给 merge/quorum 决策。
- meta 修改系统本身，失败必须回滚，不能半应用。

Trade-off：

- 单一全局策略更简单，但会让某些组合不自然。
- 按组合默认，同时暴露 `errorMode`，实现复杂一点但行为更准确。

### 8.5 Trace 存储：内存优先 vs 持久化优先

决策：MVP 内存优先，接口持久化优先。

为什么：

- Phase 0 需要快速验证 loop/context/trace 语义，`InMemoryTraceStore` 最快。
- Trace 是 evolution 原料，不能把 API 设计成只能内存使用；因此从第一天定义 `TraceStore` 接口。
- Phase 1/2 后增加 JSONL store，不破坏 public API。

Trade-off：

- MVP 运行结束后 trace 默认丢失，长期学习需要显式配置 store。
- 但先做数据库会拖慢核心语义验证，收益不如先稳定接口。

## 9. MVP 实现路线图

### Phase 0：核心类型 + MinimalLoop + 基本 trace

实现内容：

- `core` 类型：`Result`, `LoomError`, `Context`, `Trace`, `MinimalLoopDefinition`。
- `runtime/create`, `runtime/step`, `runtime/done`, `runtime/run`。
- `InMemoryTraceStore`。
- 一个 `minimal-loop.ts` 示例。

验收测试：

- 创建一个 1-step loop，`step()` 返回新 context 和 pass trace。
- `done()` 在 goal criteria 满足后返回 true。
- thrown error 被转成 `Result.err`，并写入 fail trace。
- `AbortSignal` 取消后返回 `ABORTED`，trace outcome 为 `cancelled`。

### Phase 1：Context 五层 + project/emit/merge

实现内容：

- `project()`, `emit()`, `merge()`。
- `ContextPatch` apply。
- read-only knowledge view。
- merge conflict detection。

验收测试：

- child context 有自己的 identity 和 goal。
- child state 默认为空。
- child 只能看到 affordance subset。
- child 尝试修改 inherited knowledge 在 dev mode 报错。
- merge 默认只 append parent state，不自动合并 knowledge。

### Phase 2：chain + nest 组合

实现内容：

- `composition/chain.ts`。
- `composition/nest.ts`。
- composite trace 生成。
- chain/nest examples。

验收测试：

- chain 中 A 的 output context 成为 B 的 input context。
- chain 默认 fail-fast，B 不执行。
- nest 生成 project 和 child trace。
- nest child output 通过 merge 写回 parent state。

### Phase 3：fork 并行组合

实现内容：

- `composition/fork.ts`。
- promise pool concurrency。
- fork trace tagging。
- collect-errors/quorum/fail-fast modes。

验收测试：

- N 个 slice 并发执行，最大并发不超过配置。
- sibling context 互相不可见。
- collect-errors 下部分失败仍调用 `mergeOutputs`。
- quorum 不满足时返回 `LOOP_FAILED` 或专用 quorum error。

### Phase 4：meta 演化引擎 Level 1

实现内容：

- `MutationBundle` 和 `ContextMutation`。
- `LoopRegistry` version transaction。
- Level 1 trigger：gap -> add knowledge / refine identity / refine affordance description。
- evaluator 基础评分。

验收测试：

- trace 中 repeated gap 触发 Level 1 mutation。
- mutation 产生新 context/loop version，不修改旧 version。
- evaluator 拒绝 mutation 时 rollback。
- 接受 mutation 后后续 run 能读取新增 heuristic。

### Phase 5：Level 2/3 演化

实现内容：

- `LoopMutation` with implementation refs。
- `StructureMutation` with composition graph patch。
- structure validation。
- shadow evaluation。

验收测试：

- surprise 触发 Level 2 candidate。
- invalid implementation ref 被拒绝。
- repeated timeout 触发 Level 3 decision。
- graph patch 插入 validation loop 后 trace tree 结构正确。
- regression evaluator 检测 high severity regression 并 rollback。

### Phase 6：Trace 树 + 查询 API

实现内容：

- `TraceReader.tree/path/summarize`。
- `JsonlTraceStore`。
- snapshot sampling。
- archive manifest。

验收测试：

- nested trace 能按 rootTraceId 重建树。
- fork trace 能按 forkIndex 查询。
- includeSnapshots=false 不返回 inline snapshot。
- JSONL store append 后重启进程可读取 trace。
- archive 后 manifest hash 校验通过。

## 实现原则摘要

- Loop definition 不可变，mutation 产生新 version。
- Context 不原地修改，所有变化通过 patch 产生新 context。
- Trace 是非可选输出，失败和取消也必须 trace。
- Context 是唯一信息通道，tool handler 等 side effect 通过 affordance registry 引用。
- 组合模式必须保持 closure property：chain/nest/fork/meta 的结果仍然是 Loop。
