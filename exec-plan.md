# Loom Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox syntax for phase acceptance tracking.

**Goal:** Build Loom as a TypeScript framework where Everything is a Loop, Context is the universal carrier, Trace is first-class, and evolution rewrites loops through versioned mutations.

**Architecture:** `core` owns serializable contracts and immutable data operations. `observability` stores and reads Trace data. `runtime` implements `create/step/done/run`. `composition` builds `chain/nest/fork/meta` loops on top of runtime. `evolution` manages mutation decisions, versioned registry transactions, evaluation, and rollback.

**Tech Stack:** TypeScript, ESM Node.js, Vitest, ESLint flat config, JSONL for local persistent Trace storage.

---

## 项目初始化步骤

1. Create directory layout:
   `src/core`, `src/runtime`, `src/observability`, `src/composition`, `src/evolution`, `src/examples`, `tests/core`, `tests/runtime`, `tests/observability`, `tests/composition`, `tests/evolution`, `tests/integration`.
2. Create `package.json` with `type: "module"` and scripts:
   `build`, `typecheck`, `test`, `test:watch`, `lint`, `format`.
3. Create `tsconfig.json` with `strict: true`, `declaration: true`, `moduleResolution: "NodeNext"`, `module: "NodeNext"`, `target: "ES2022"`, `rootDir: "src"`, `outDir: "dist"`.
4. Create `vitest.config.ts` with Node environment and coverage for `src/**/*.ts`.
5. Create `eslint.config.mjs` using `@eslint/js` and `typescript-eslint`, enforcing no implicit `any`, no floating promises, and consistent type exports.
6. Create `src/index.ts` as the public barrel; each task below must add exports through the nearest module `index.ts` and then through `src/index.ts`.
7. After every task run: `npm run typecheck`, `npm test -- <task-test-file>`, and `npm run lint`.

## 文件结构边界

- `src/core/*`: JSON-compatible types, branded IDs, `Result`, `LoomError`, immutable Context, MinimalLoop, Trace contracts. No imports from other Loom modules.
- `src/observability/*`: `TraceStore`, in-memory store, JSONL store, `TraceReader`, sampling, archive. May import only from `core`.
- `src/runtime/*`: `create`, `step`, `done`, `run`, cancellation, registry, scheduler. May import from `core` and `observability`.
- `src/composition/*`: `chain`, `nest`, `fork`, `meta`, composition graph. Uses public runtime APIs for child loops.
- `src/evolution/*`: mutation data structures, triggers, strategy, evaluator, registry transactions, engine. May depend on `composition`, `runtime`, `observability`, and `core`.
- `src/examples/*`: runnable examples; never imported by framework modules.

## Phase 0: 核心类型 + MinimalLoop + 基本 Trace

### 目标
Build the minimal TypeScript framework surface: core contracts, uniform loop runtime, in-memory Trace collection, and a runnable one-step loop.

### 验收标准
- [ ] A one-step `MinimalLoopDefinition` can be created, stepped, marked done, and run to completion.
- [ ] `step()` returns a new immutable Context and a `pass` Trace.
- [ ] `done()` returns `true` when a required success criterion evaluator passes or a budget stop condition is reached.
- [ ] A thrown loop-author error is converted to `Result.err` with `LoomError.code = "INTERNAL"` and persisted as a `fail` Trace.
- [ ] `AbortSignal` cancellation returns `ABORTED` and persists a Trace with `outcome = "cancelled"`.

### 任务清单

#### Task 0.1: 项目脚手架
- **文件**: Create `package.json`, `tsconfig.json`, `vitest.config.ts`, `eslint.config.mjs`, `src/index.ts`, `tests/setup.ts`
- **导出接口**: None; configuration task
- **测试**: `tests/setup.test.ts`
- **依赖**: None
- **可并行**: Yes; can run before all implementation tasks
- **描述**: Initialize the repository as a strict ESM TypeScript package with test, lint, build, and typecheck commands.
- **关键实现点**: `package.json` scripts must include `typecheck: tsc --noEmit`, `test: vitest run`, `build: tsc -p tsconfig.json`, `lint: eslint .`; Vitest must include `tests/setup.ts`; `src/index.ts` starts as an empty public barrel.
- **预估复杂度**: S

#### Task 0.2: Branded IDs, JSON values, Result, and LoomError
- **文件**: Create `src/core/ids.ts`, `src/core/json.ts`, `src/core/result.ts`, `src/core/errors.ts`, `src/core/index.ts`; modify `src/index.ts`
- **导出接口**: `Brand`, `LoopId`, `LoopVersion`, `ContextId`, `TraceId`, `RunId`, `StepNumber`, `ISODateTime`, `DurationMs`, `newLoopId`, `newLoopVersion`, `newContextId`, `newTraceId`, `newRunId`, `asStepNumber`, `JsonPrimitive`, `JsonValue`, `Metadata`, `Result`, `ok`, `err`, `isOk`, `isErr`, `LoomError`, `makeLoomError`, `toLoomError`
- **测试**: `tests/core/ids-result-errors.test.ts`
- **依赖**: Task 0.1
- **可并行**: No; unblocks all core type work
- **描述**: Implement serializable primitive boundaries and the canonical expected-failure representation.
- **关键实现点**: ID factories return branded strings with stable prefixes such as `loop_`, `ctx_`, `trace_`, `run_`; `LoopVersion` starts as `v1` and registry tasks later increment it; `toLoomError()` maps non-`LoomError` exceptions to `INTERNAL`, `retryable: false`, and JSON-safe `cause`.
- **预估复杂度**: S

#### Task 0.3: Immutable utilities and Context five-layer contracts
- **文件**: Create `src/core/immutable.ts`, `src/core/context.ts`; modify `src/core/index.ts`, `src/index.ts`
- **导出接口**: `Immutable`, `MaybePromise`, `deepFreeze`, `freezeContext`, `Constraint`, `Capability`, `IdentityLayer`, `SuccessCriterion`, `Budget`, `GoalLayer`, `Observation`, `Decision`, `PendingLoop`, `StateLayer`, `KnowledgeItem`, `KnowledgeLayer`, `ToolRef`, `LoopRef`, `ResourceRef`, `AffordanceLayer`, `EvaluatorRef`, `ProjectionRef`, `ImplementationRef`, `Context`, `AnyContext`, `ContextPatch`, `PatchOperation`, `Action`, `emptyState`, `emptyKnowledge`, `emptyAffordances`
- **测试**: `tests/core/context-types.test.ts`
- **依赖**: Task 0.2
- **可并行**: No; required by MinimalLoop and Trace
- **描述**: Encode the five Context layers from `design.md` as immutable JSON-compatible contracts.
- **关键实现点**: `Context` must require `identity`, `goal`, `state`, `knowledge`, and `affordances`; `state` arrays are append-only by convention and copied by patch operations; `knowledge` stores `facts`, `heuristics`, and `memories` while every item keeps a `kind`; `freezeContext()` recursively freezes all five layers and preserves structural sharing for unchanged nested objects.
- **预估复杂度**: M

#### Task 0.4: MinimalLoop contracts and runtime-facing type interfaces
- **文件**: Create `src/core/loop.ts`; modify `src/core/index.ts`, `src/index.ts`
- **导出接口**: `MinimalLoopDefinition`, `LoopHandle`, `StepResult`, `StepFunction`, `DoneFunction`, `RuntimeRegistry`, `ToolRegistry`, `ToolHandler`, `LoopRegistryView`, `EvaluatorRegistry`, `CriterionEvaluator`, `ImplementationRegistry`, `StepRuntime`, `DoneRuntime`, `ToolCallOptions`, `RunOptions`, `RunMetrics`, `RunResult`, `CreateOptions`, `StepOptions`, `DoneOptions`, `TraceOptions`
- **测试**: `tests/core/loop-contracts.test.ts`
- **依赖**: Task 0.3
- **可并行**: Yes, after Task 0.3; can run in parallel with Task 0.5
- **描述**: Define the immutable loop definition, runtime handle, step/done function signatures, and uniform API option types.
- **关键实现点**: `StepFunction` returns `Promise<Result<StepResult>>`; `DoneFunction` returns `MaybePromise<Result<boolean>>`; `MinimalLoopDefinition` stores versioned immutable `identity` and `goal`; `LoopHandle` binds `definition`, `traceReader`, and `createdAt` without mutating the definition.
- **预估复杂度**: M

#### Task 0.5: Trace base types and in-memory storage
- **文件**: Create `src/core/trace.ts`, `src/observability/trace-store.ts`, `src/observability/in-memory-trace-store.ts`, `src/observability/index.ts`; modify `src/core/index.ts`, `src/index.ts`
- **导出接口**: `TraceOutcome`, `Trace`, `TraceSnapshot`, `TraceEvent`, `TraceSink`, `TraceStore`, `TraceQuery`, `InMemoryTraceStore`, `createInMemoryTraceSink`
- **测试**: `tests/observability/in-memory-trace-store.test.ts`
- **依赖**: Task 0.3
- **可并行**: Yes, after Task 0.3; can run in parallel with Task 0.4
- **描述**: Make Trace first-class and provide the Phase 0 storage backend.
- **关键实现点**: `InMemoryTraceStore` uses `Map<TraceId, Trace>` plus secondary arrays for query filtering by `runId`, `loopId`, `rootTraceId`, `parentTraceId`, `outcome`, and `tags`; `appendEvent()` records streaming events separately and must accept `step.completed` by also appending its complete Trace; `children(id)` yields traces where `parentTraceId === id`.
- **预估复杂度**: M

#### Task 0.6: Uniform runtime API: create, step, done, run
- **文件**: Create `src/runtime/cancellation.ts`, `src/runtime/registry.ts`, `src/runtime/create.ts`, `src/runtime/step.ts`, `src/runtime/done.ts`, `src/runtime/run.ts`, `src/runtime/index.ts`; modify `src/index.ts`
- **导出接口**: `composeAbort`, `defaultRuntimeRegistry`, `createRuntimeRegistry`, `create`, `step`, `done`, `run`, `stepStream`
- **测试**: `tests/runtime/minimal-loop-runtime.test.ts`, `tests/runtime/error-and-cancellation.test.ts`
- **依赖**: Task 0.4, Task 0.5
- **可并行**: No; it is the Phase 0 runtime critical path
- **描述**: Implement the four uniform operations from `design.md` with timeout, cancellation, trace persistence, and error conversion.
- **关键实现点**: `create()` validates definition fields and attaches a `TraceReader` backed by the selected `TraceStore`; `step()` emits `step.started`, calls `definition.step`, freezes the returned Context, appends `step.completed`, and persists success/failure Trace; thrown exceptions are converted by `toLoomError()`; `done()` checks abort/timeout and delegates to `definition.done`; `run()` loops until `done()` is true or `maxSteps`/budget is exceeded, returning `RunResult`.
- **预估复杂度**: L

#### Task 0.7: Minimal loop example and public barrel verification
- **文件**: Create `src/examples/minimal-loop.ts`; modify `src/index.ts`
- **导出接口**: `makeMinimalCounterLoop`, `makeInitialCounterContext`
- **测试**: `tests/integration/minimal-loop.example.test.ts`
- **依赖**: Task 0.6
- **可并行**: No
- **描述**: Provide a runnable example proving the public API can create and run a one-step loop.
- **关键实现点**: Example loop appends an `Observation` with counter value, records a `Decision`, stops when `state.observations.length >= goal.budget.maxSteps`, and uses only public exports from `src/index.ts`.
- **预估复杂度**: S

## Phase 1: Context 五层 + project/emit/merge

### 目标
Implement Context boundary operations so parent loops can project scoped child Contexts, receive child outputs, and merge results immutably.

### 验收标准
- [ ] `project()` creates a child Context with its own `identity` and projected `goal`.
- [ ] Child `state` is empty unless `includeStateSummary` is explicitly enabled.
- [ ] Child `affordances` are a subset of the parent selected by an allowlist filter.
- [ ] In dev mode, attempts to mutate inherited knowledge fail.
- [ ] `merge()` defaults to appending parent state and does not automatically merge child knowledge.

### 任务清单

#### Task 1.1: ContextPatch apply and structural sharing
- **文件**: Create `src/core/context-patch.ts`; modify `src/core/context.ts`, `src/core/index.ts`, `src/index.ts`
- **导出接口**: `applyContextPatch`, `validateContextPatch`, `ContextPatchError`, `makeContextSnapshotHash`
- **测试**: `tests/core/context-patch.test.ts`
- **依赖**: Task 0.3
- **可并行**: Yes, after Phase 0; can run with Task 1.4
- **描述**: Implement the only supported way to produce new Context versions from an existing Context.
- **关键实现点**: Validate `baseContextId`; handle `appendObservation`, `appendDecision`, `appendPending`, `clearPending`, `addKnowledge`, `replaceGoal`, `replaceIdentity`, `replaceAffordances`, `setMetadata`; copy only changed arrays/layers; route all outputs through `freezeContext()`; reject invalid operation shapes with `VALIDATION_FAILED`.
- **预估复杂度**: M

#### Task 1.2: project(parent, child_goal)
- **文件**: Create `src/core/context-boundary.ts`; modify `src/core/index.ts`, `src/index.ts`
- **导出接口**: `ProjectOptions`, `ProjectResult`, `project`, `selectKnowledge`, `selectAffordances`, `summarizeParentState`
- **测试**: `tests/core/project.test.ts`
- **依赖**: Task 1.1, Task 0.5
- **可并行**: No; depends on patch and Trace contracts
- **描述**: Generate scoped child Contexts following the five-layer scoping rules in `design.md`.
- **关键实现点**: Validate child identity and child goal; assign a fresh `ContextId`; preserve parent `runId`; set `parentContextId`; build empty child `state` unless `includeStateSummary`; filter inherited knowledge read-only; filter tools/loops/resources by `affordanceFilter`; create a boundary Trace with `kind = "project"` and selected IDs in metadata.
- **预估复杂度**: M

#### Task 1.3: emit(child_ctx_out)
- **文件**: Modify `src/core/context-boundary.ts`, `src/core/index.ts`, `src/index.ts`
- **导出接口**: `ChildOutput`, `EmitOptions`, `emit`, `defaultObservationSelector`, `defaultKnowledgeSelector`
- **测试**: `tests/core/emit.test.ts`
- **依赖**: Task 1.2, Task 0.6
- **可并行**: No
- **描述**: Extract child results without directly modifying the parent Context.
- **关键实现点**: `emit(childContext, childLoop, status, options)` filters observations and knowledge candidates; copies child decisions; computes `RunMetrics` from trace reader/store data; includes `traceRootId` when available; freezes the returned `ChildOutput`.
- **预估复杂度**: M

#### Task 1.4: Knowledge read-only view and dev mutation guard
- **文件**: Create `src/core/knowledge.ts`; modify `src/core/index.ts`, `src/index.ts`
- **导出接口**: `KnowledgeQuery`, `KnowledgeView`, `KnowledgeWriter`, `KnowledgeAccess`, `createKnowledgeView`, `createReadOnlyKnowledgeAccess`, `createDevKnowledgeProxy`
- **测试**: `tests/core/knowledge-view.test.ts`
- **依赖**: Task 0.3
- **可并行**: Yes, after Phase 0; can run with Task 1.1
- **描述**: Provide read-only inherited knowledge semantics for child loops.
- **关键实现点**: `KnowledgeView.search()` filters by text, kind, min confidence, source trace id, tags, and limit; `createReadOnlyKnowledgeAccess()` exposes only `read`; `createDevKnowledgeProxy()` throws an invariant error on mutation attempts while keeping production mode proxy-free.
- **预估复杂度**: M

#### Task 1.5: merge(parent_ctx, child_output)
- **文件**: Create `src/core/merge.ts`; modify `src/core/context-boundary.ts`, `src/core/index.ts`, `src/index.ts`
- **导出接口**: `MergePolicy`, `MergeConflict`, `detectMergeConflict`, `defaultMergePolicy`, `makeChildSummaryObservation`, `merge`
- **测试**: `tests/core/merge.test.ts`
- **依赖**: Task 1.1, Task 1.3, Task 1.4
- **可并行**: No
- **描述**: Widen child output back into parent scope through explicit policy.
- **关键实现点**: Default policy uses `appendChildSummaryObservation: true`, `onConflict: "reject"`, and no automatic knowledge acceptance; detect duplicate accepted knowledge IDs and conflicting pending loop IDs; construct a `ContextPatch` that appends summary observation, selected observations, child decisions, and accepted knowledge; never replace parent identity, goal, or affordances.
- **预估复杂度**: L

#### Task 1.6: Context scoping integration example
- **文件**: Create `src/examples/nested-tool.ts`; create `tests/integration/context-boundary.test.ts`
- **导出接口**: `makeParentContextForBoundaryExample`, `makeChildGoalForBoundaryExample`
- **测试**: `tests/integration/context-boundary.test.ts`
- **依赖**: Task 1.5
- **可并行**: No
- **描述**: Demonstrate `project -> emit -> merge` without using `nest` yet.
- **关键实现点**: Parent has two tools, two resources, and three knowledge items; child receives exactly one tool/resource and selected knowledge; emitted child knowledge remains a candidate until merge policy accepts it.
- **预估复杂度**: S

## Phase 2: chain + nest 组合

### 目标
Implement sequential and hierarchical composition while preserving Context flow, boundary traces, and predictable error propagation.

### 验收标准
- [ ] In `chain`, loop A's output Context becomes loop B's input Context.
- [ ] `chain` defaults to fail-fast and does not execute downstream loops after upstream failure.
- [ ] `nest` emits project, child, and composite nest traces.
- [ ] `nest` merges child output back into parent state through the configured merge function.

### 任务清单

#### Task 2.1: Composite trace helpers
- **文件**: Create `src/composition/composite-trace.ts`, `src/composition/index.ts`; modify `src/index.ts`
- **导出接口**: `CompositeKind`, `makeCompositeTrace`, `makeFailureTrace`, `linkChildTrace`, `collectChildTraceIds`
- **测试**: `tests/composition/composite-trace.test.ts`
- **依赖**: Task 0.5
- **可并行**: Yes, after Phase 0; can run before Task 2.2 and Task 2.3
- **描述**: Centralize construction of `chain`, `nest`, `fork`, and `meta` Trace records.
- **关键实现点**: Composite traces preserve `rootTraceId`, assign `parentTraceId` to child traces when provided, copy `inputContextId` and final `outputContextId`, set `children` from child Trace IDs, and put composition-specific metadata in `metadata.composition`.
- **预估复杂度**: M

#### Task 2.2: chain composition
- **文件**: Create `src/composition/chain.ts`; modify `src/composition/index.ts`, `src/index.ts`
- **导出接口**: `ChainOptions`, `chain`
- **测试**: `tests/composition/chain.test.ts`
- **依赖**: Task 0.6, Task 2.1
- **可并行**: Yes, after Task 2.1; independent from Task 2.3
- **描述**: Compose multiple LoopHandles into one LoopHandle whose step runs children sequentially.
- **关键实现点**: `chain(loops, options)` returns `Result<LoopHandle>`; composite step iterates loops in order, calls `done(child, current)` before stepping, calls `step(child, current)` when not done, updates `current` to child output Context, stores child traces, returns last non-undefined child output; default `errorMode` is `"fail-fast"`, while `"continue"` records failure traces and proceeds with the latest valid Context.
- **预估复杂度**: L

#### Task 2.3: nest composition
- **文件**: Create `src/composition/nest.ts`; modify `src/composition/index.ts`, `src/index.ts`
- **导出接口**: `NestOptions`, `nest`
- **测试**: `tests/composition/nest.test.ts`
- **依赖**: Task 1.5, Task 2.1
- **可并行**: Yes, after Task 2.1 and Phase 1; independent from Task 2.2
- **描述**: Compose an outer loop and inner loop where the outer step may invoke the inner through explicit Context boundary functions.
- **关键实现点**: `nest(outer, inner, options)` runs `outer.step`, evaluates `when`, runs configured `project`, emits `child.started`, calls `run(inner, childContext)`, converts child failure to child status, calls configured `emit` and `merge`, then returns a `kind = "nest"` composite Trace containing outer, project, and child traces; default `errorMode` is `"propagate"`.
- **预估复杂度**: L

#### Task 2.4: chain and nest examples
- **文件**: Create `src/examples/chain-pipeline.ts`; modify `src/examples/nested-tool.ts`
- **导出接口**: `makeChainPipelineExample`, `makeNestedToolExample`
- **测试**: `tests/integration/chain-nest.examples.test.ts`
- **依赖**: Task 2.2, Task 2.3
- **可并行**: No
- **描述**: Provide public examples for pipeline and parent-child loop boundaries.
- **关键实现点**: Chain example uses `retrieve -> analyze -> summarize` loops with append-only observations; nest example uses a parent loop that invokes a tool-like child only when a pending observation is present, then merges a child summary observation.
- **预估复杂度**: S

## Phase 3: fork 并行组合

### 目标
Implement parallel loop composition with isolated sibling Contexts, bounded concurrency, configurable merge strategy, and quorum behavior.

### 验收标准
- [ ] N slices execute with observed max concurrency not exceeding `ForkOptions.concurrency`.
- [ ] Sibling Contexts cannot observe each other's state during execution.
- [ ] In `collect-errors` mode, partial failures are recorded and `mergeOutputs` still runs with successful outputs.
- [ ] In `quorum` mode, insufficient successful outputs return a `LOOP_FAILED` error with quorum metadata.

### 任务清单

#### Task 3.1: Promise worker pool scheduler
- **文件**: Create `src/runtime/scheduler.ts`; modify `src/runtime/index.ts`, `src/index.ts`
- **导出接口**: `PromisePool`, `createPromisePool`, `defaultConcurrency`, `PoolTaskResult`
- **测试**: `tests/runtime/scheduler.test.ts`
- **依赖**: Task 0.6
- **可并行**: Yes, after Phase 0
- **描述**: Provide bounded in-process concurrency for fork workers.
- **关键实现点**: Scheduler accepts async task factories, starts at most `concurrency` tasks, preserves original slice index in results, stops scheduling new tasks when the shared signal aborts, and records `maxObservedConcurrency` for tests and Trace metadata.
- **预估复杂度**: M

#### Task 3.2: fork composition execution
- **文件**: Create `src/composition/fork.ts`; modify `src/composition/index.ts`, `src/index.ts`
- **导出接口**: `ForkOptions`, `fork`, `ForkWorkerResult`, `tagTraceSink`
- **测试**: `tests/composition/fork.execution.test.ts`
- **依赖**: Task 1.5, Task 2.1, Task 3.1
- **可并行**: No
- **描述**: Implement fan-out execution over slices using projected child Contexts.
- **关键实现点**: `split(context)` returns JSON-compatible slices; `projectSlice(context, slice, index)` creates isolated child Context; each worker calls `run(worker, childContext, { timeoutMs: workerTimeoutMs })`; `tagTraceSink()` adds `forkIndex` to emitted Trace metadata; outputs are produced via `emit(child.context, worker, "completed")`.
- **预估复杂度**: L

#### Task 3.3: fork error modes, merge strategy, and quorum
- **文件**: Modify `src/composition/fork.ts`
- **导出接口**: `ForkErrorMode`, `makeQuorumError`, `makeForkTrace`
- **测试**: `tests/composition/fork.error-modes.test.ts`
- **依赖**: Task 3.2
- **可并行**: No
- **描述**: Complete `collect-errors`, `fail-fast`, and `quorum` semantics.
- **关键实现点**: Default mode is `collect-errors`; `fail-fast` aborts unscheduled workers and returns first error; `quorum` requires `outputs.length >= quorum ?? sliceCount`; `makeQuorumError()` returns `LoomError.code = "LOOP_FAILED"` with `metadata.required`, `metadata.actual`, and JSON-safe child error summaries; `mergeOutputs(parent, outputs)` is called only when the selected mode allows continuation.
- **预估复杂度**: M

#### Task 3.4: fork reviewer example
- **文件**: Create `src/examples/fork-reviewers.ts`
- **导出接口**: `makeForkReviewersExample`, `makeReviewSliceContext`
- **测试**: `tests/integration/fork-reviewers.example.test.ts`
- **依赖**: Task 3.3
- **可并行**: No
- **描述**: Demonstrate map-reduce semantics for parallel review slices.
- **关键实现点**: Split a file list into slices; each child receives only its file resource and reviewer identity; merge outputs append one parent observation per finding and one summary observation with success/failure counts.
- **预估复杂度**: S

## Phase 4: meta 演化引擎 Level 1

### 目标
Implement the first evolution layer: trace-driven Context mutations, versioned registry transactions, cheap-level decision logic, and evaluator-based acceptance.

### 验收标准
- [ ] Repeated `gap` Trace signals produce a Level 1 `ContextMutation` decision.
- [ ] Applying a mutation creates a new version and never modifies the old version in place.
- [ ] Evaluator rejection rolls back the mutation transaction.
- [ ] Accepted Level 1 mutation makes the next run able to read the added heuristic or refined Context data.

### 任务清单

#### Task 4.1: Mutation data model
- **文件**: Create `src/evolution/mutations.ts`, `src/evolution/index.ts`; modify `src/index.ts`
- **导出接口**: `MutationBundle`, `Mutation`, `ContextMutation`, `LoopMutation`, `StructureMutation`, `CompositionGraphPatch`, `ExpectedImpact`, `MutationPolicy`, `validateMutationBundleShape`
- **测试**: `tests/evolution/mutations.test.ts`
- **依赖**: Task 1.1
- **可并行**: Yes, after Phase 1; can run with Task 4.3 and Task 4.4
- **描述**: Represent auditable, trace-linked mutations for Levels 1, 2, and 3 while initially validating Level 1.
- **关键实现点**: `MutationBundle.baseVersion` must be required; `createdFromTraceIds` must be non-empty when policy `requireEvidenceTrace` is true; Level 1 mutation holds a `ContextPatch`; Level 2 and 3 types are defined now but their deep validation is completed in Phase 5.
- **预估复杂度**: M

#### Task 4.2: Versioned LoopRegistry transactions
- **文件**: Create `src/evolution/registry.ts`; modify `src/evolution/index.ts`, `src/index.ts`
- **导出接口**: `LoopRegistry`, `MutationTransaction`, `InMemoryLoopRegistry`, `VersionedLoopRecord`, `versionMismatch`
- **测试**: `tests/evolution/loop-registry.test.ts`
- **依赖**: Task 0.6, Task 4.1
- **可并行**: No
- **描述**: Add immutable loop version management and transaction boundaries for evolution.
- **关键实现点**: Registry stores `Map<LoopId, Map<LoopVersion, LoopHandle>>` plus active version pointer; `beginMutation(loopId, baseVersion)` rejects if base is not active; `apply()` creates a candidate handle with incremented version and staged active pointer; `commit()` publishes staged version; `rollback()` discards staged candidate.
- **预估复杂度**: L

#### Task 4.3: Level 1 triggers and evolution strategy
- **文件**: Create `src/evolution/triggers.ts`, `src/evolution/strategy.ts`; modify `src/evolution/index.ts`, `src/index.ts`
- **导出接口**: `EvolutionTrigger`, `EvolutionDecision`, `defaultEvolutionTriggers`, `decideEvolution`, `groupTraceSignals`, `detectStructuralSignals`
- **测试**: `tests/evolution/strategy.level1.test.ts`
- **依赖**: Task 0.5
- **可并行**: Yes, after Phase 0; can run with Task 4.1
- **描述**: Implement the cheapest-level-first decision tree for trace summaries and raw traces.
- **关键实现点**: Repeated `gap` text chooses Level 1 with `proposedMutationKinds = ["context"]`; repeated `surprise` chooses Level 2 but is not applied until Phase 5; repeated timeouts or cross-loop failures choose Level 3; weak signals default to Level 1 context refinement.
- **预估复杂度**: M

#### Task 4.4: Evolution evaluator baseline
- **文件**: Create `src/evolution/evaluator.ts`; modify `src/evolution/index.ts`, `src/index.ts`
- **导出接口**: `EvolutionEvaluation`, `EvolutionEvaluator`, `Regression`, `ScoreRunOptions`, `scoreRun`, `DefaultEvolutionEvaluator`
- **测试**: `tests/evolution/evaluator.test.ts`
- **依赖**: Task 0.5
- **可并行**: Yes, after Phase 0; can run with Task 4.1
- **描述**: Provide the default compare-and-accept evaluator for candidate mutations.
- **关键实现点**: `scoreRun()` computes pass rate minus fail, timeout, duration, and gap penalties; `DefaultEvolutionEvaluator.compare()` accepts only when `scoreAfter >= scoreBefore + minDelta`, no high-severity regression exists, mutation risk does not exceed policy, and evidence trace IDs cover the target failure type.
- **预估复杂度**: M

#### Task 4.5: meta Level 1 composition and engine
- **文件**: Create `src/evolution/engine.ts`; create `src/composition/meta.ts`; modify `src/evolution/index.ts`, `src/composition/index.ts`, `src/index.ts`
- **导出接口**: `MetaOptions`, `meta`, `EvolutionEngine`, `EvolutionEngineOptions`, `buildEvolutionContext`, `applyLevel1Mutation`
- **测试**: `tests/evolution/meta-level1.test.ts`, `tests/composition/meta.test.ts`
- **依赖**: Task 4.2, Task 4.3, Task 4.4
- **可并行**: No
- **描述**: Run an inner loop, analyze its traces through an evolver loop, validate and apply Level 1 mutations, then accept or roll back.
- **关键实现点**: `meta(inner, evolver, options)` creates a composed loop whose step runs the active inner version, builds evolution Context from traces, runs the evolver to produce `MutationBundle`, validates policy, starts registry transaction, applies context patch as a new version, compares candidate through evaluator, commits on acceptance, otherwise rolls back and returns a rejected meta Trace without changing active version.
- **预估复杂度**: XL

#### Task 4.6: Level 1 evolution example
- **文件**: Create `src/examples/level1-evolution.ts`
- **导出接口**: `makeLevel1EvolutionExample`, `makeGapProducingLoop`, `makeHeuristicEvolverLoop`
- **测试**: `tests/integration/level1-evolution.example.test.ts`
- **依赖**: Task 4.5
- **可并行**: No
- **描述**: Demonstrate repeated gap traces causing a heuristic to be added through Level 1 evolution.
- **关键实现点**: Inner loop emits repeated `gap = "permission ownership unknown"`; evolver loop outputs a `ContextMutation` adding `heuristic.permission-ownership-first`; accepted mutation makes the subsequent run find the heuristic in `knowledge.heuristics`.
- **预估复杂度**: S

## Phase 5: Level 2/3 演化

### 目标
Implement loop implementation mutation, structure mutation, graph validation, and shadow evaluation with rollback on regression.

### 验收标准
- [ ] Repeated `surprise` Trace signals produce a Level 2 candidate mutation.
- [ ] Invalid `ImplementationRef` is rejected before transaction commit.
- [ ] Repeated timeouts can produce a Level 3 structure decision.
- [ ] Inserting a validation loop through a graph patch produces the expected Trace tree.
- [ ] Shadow evaluation detects high-severity regression and rolls back the candidate.

### 任务清单

#### Task 5.1: Implementation refs and LoopMutation apply
- **文件**: Create `src/evolution/loop-mutation.ts`; modify `src/runtime/registry.ts`, `src/evolution/registry.ts`, `src/evolution/index.ts`, `src/index.ts`
- **导出接口**: `applyLoopMutation`, `validateImplementationRef`, `InMemoryImplementationRegistry`, `registerImplementation`
- **测试**: `tests/evolution/loop-mutation.test.ts`
- **依赖**: Task 4.2
- **可并行**: Yes, after Phase 4; can run with Task 5.2
- **描述**: Apply Level 2 mutations by replacing registered step/done implementations or immutable definition fields.
- **关键实现点**: Only registered `ImplementationRef` values may replace `step` or `done`; invalid refs return `MUTATION_REJECTED`; identity/goal patches reuse `PatchOperation` validation where possible; candidate loop receives a new `LoopVersion` while old definition remains unchanged.
- **预估复杂度**: L

#### Task 5.2: Composition graph and StructureMutation validation
- **文件**: Create `src/composition/graph.ts`, `src/evolution/structure-mutation.ts`; modify `src/composition/index.ts`, `src/evolution/index.ts`, `src/index.ts`
- **导出接口**: `CompositionGraph`, `CompositionNode`, `CompositionEdge`, `GraphValidationError`, `validateCompositionGraph`, `applyStructureMutation`, `validateGraphPatch`
- **测试**: `tests/evolution/structure-mutation.test.ts`, `tests/composition/graph.test.ts`
- **依赖**: Task 4.1, Task 2.3, Task 3.3
- **可并行**: Yes, after Phase 4 and composition phases; can run with Task 5.1
- **描述**: Represent loop structures and safely apply Level 3 graph patches.
- **关键实现点**: Graph validation rejects missing nodes, dangling edges, cycles in `chain` composition, invalid `nest` parent-child boundaries, `fork` nodes without split/merge metadata, and `meta` nodes that target themselves without explicit recursion metadata; `insert-loop`, `remove-loop`, `replace-loop`, and `change-composition` return a new graph version.
- **预估复杂度**: L

#### Task 5.3: Shadow evaluation runner
- **文件**: Create `src/evolution/shadow.ts`; modify `src/evolution/evaluator.ts`, `src/evolution/index.ts`, `src/index.ts`
- **导出接口**: `ShadowEvaluationOptions`, `ShadowEvaluationResult`, `runShadowEvaluation`, `compareShadowSummaries`
- **测试**: `tests/evolution/shadow-evaluation.test.ts`
- **依赖**: Task 4.4, Task 5.1
- **可并行**: No
- **描述**: Evaluate candidate versions without changing the active version or leaking side effects into the primary Trace stream.
- **关键实现点**: Run before and candidate handles against cloned/frozen Context snapshots; use a separate `RunId` and TraceStore namespace tagged `shadow: true`; compare summaries with `DefaultEvolutionEvaluator`; mark high-severity regressions when pass rate decreases beyond threshold, timeout rate increases, or required criteria stop passing.
- **预估复杂度**: L

#### Task 5.4: Level 2/3 engine integration
- **文件**: Modify `src/evolution/engine.ts`, `src/composition/meta.ts`
- **导出接口**: `applyMutationBundle`, `validateMutationPolicy`, `selectMutationApplier`
- **测试**: `tests/evolution/meta-level2-level3.test.ts`
- **依赖**: Task 5.1, Task 5.2, Task 5.3
- **可并行**: No
- **描述**: Extend meta evolution from Level 1 to Level 2 and Level 3 with policy validation and shadow evaluation.
- **关键实现点**: `selectMutationApplier()` dispatches by mutation level/kind; `MutationPolicy.allowCodeReplacement` gates Level 2 step/done replacement; `MutationPolicy.allowStructureChange` gates Level 3; high-risk mutations require shadow evaluation before commit; evaluator rejection or validation failure rolls back registry transaction and emits a `meta` Trace with `outcome = "skipped"` or `fail` depending on error type.
- **预估复杂度**: XL

#### Task 5.5: Level 3 validation-loop example
- **文件**: Create `src/examples/structure-evolution.ts`
- **导出接口**: `makeStructureEvolutionExample`, `makeValidationLoop`, `makeTimeoutSignalTraces`
- **测试**: `tests/integration/structure-evolution.example.test.ts`
- **依赖**: Task 5.4
- **可并行**: No
- **描述**: Demonstrate a structure mutation that inserts a validation loop after an execute loop.
- **关键实现点**: Initial graph is `plan -> execute`; repeated timeout or surprise traces produce `StructureMutation.operation = "insert-loop"` with `insertAfter = "execute"` and `loopRef = "validate"`; accepted graph becomes `plan -> execute -> validate`, and Trace tree shows the validation child after execute.
- **预估复杂度**: M

## Phase 6: Trace 树 + 查询 API

### 目标
Complete the observability layer with tree/path/summary queries, JSONL persistence, snapshot sampling, and archive manifests.

### 验收标准
- [ ] Nested traces can be rebuilt into a tree by `rootTraceId`.
- [ ] Fork traces can be queried by `forkIndex` metadata.
- [ ] `includeSnapshots = false` strips inline snapshots from reader results.
- [ ] `JsonlTraceStore` can append traces and read them back after process restart.
- [ ] Archive manifest hash validation passes after compression or no-compression archive creation.

### 任务清单

#### Task 6.1: TraceReader tree, path, and summary
- **文件**: Create `src/observability/trace-reader.ts`; modify `src/observability/index.ts`, `src/index.ts`
- **导出接口**: `TraceNode`, `TraceReader`, `TraceTreeOptions`, `TraceSummary`, `DefaultTraceReader`, `stripTraceSnapshots`
- **测试**: `tests/observability/trace-reader.test.ts`
- **依赖**: Task 2.3, Task 3.3
- **可并行**: Yes, after composition traces exist; can run with Task 6.2
- **描述**: Build read-time Trace trees and summaries from flat store records.
- **关键实现点**: `tree(rootTraceId, { maxDepth })` recursively uses `store.children(id)` and stops at `maxDepth`; `path(traceId)` walks `parentTraceId` to root and returns ordered root-to-leaf traces; `summarize(query)` counts by outcome, averages duration, collects `gap`, `surprise`, and errors; `includeSnapshots = false` removes `inputSnapshot.value` and `outputSnapshot.value`.
- **预估复杂度**: L

#### Task 6.2: JSONL TraceStore
- **文件**: Create `src/observability/jsonl-trace-store.ts`; modify `src/observability/index.ts`, `src/index.ts`
- **导出接口**: `JsonlTraceStore`, `JsonlTraceStoreOptions`, `SerializedTraceRecord`, `JsonlTraceIndex`
- **测试**: `tests/observability/jsonl-trace-store.test.ts`
- **依赖**: Task 0.5
- **可并行**: Yes, after Phase 0; can run with Task 6.1
- **描述**: Add append-only local persistent storage for traces, events, snapshots, and index records.
- **关键实现点**: Each line stores `{ type, id, runId, payload, hash }`; `append()` writes trace records atomically enough for a single process by serializing one full line at a time; large inline snapshots are extracted into `snapshot` records and referenced through `contentRef`; every N traces writes an `index` record with offset, `rootTraceId`, `loopId`, and `outcome`; constructor loads index records lazily and falls back to scanning when no index exists.
- **预估复杂度**: L

#### Task 6.3: Trace sampling and snapshot policy
- **文件**: Create `src/observability/sampling.ts`; modify `src/runtime/step.ts`, `src/observability/index.ts`, `src/index.ts`
- **导出接口**: `TraceSamplePolicy`, `defaultTraceSamplePolicy`, `shouldIncludeFullSnapshot`, `makeTraceSnapshot`, `applySamplePolicy`
- **测试**: `tests/observability/sampling.test.ts`, `tests/runtime/trace-sampling.integration.test.ts`
- **依赖**: Task 6.2, Task 1.1
- **可并行**: No
- **描述**: Control when full Context snapshots, deltas, and reasoning are written to Trace records.
- **关键实现点**: Default policy includes full snapshots on run start, run end, failure, and boundary; supports `every-n-steps`; computes snapshot hash from stable JSON serialization; if serialized snapshot exceeds `maxInlineSnapshotBytes`, store `encoding = "content-ref"` and write snapshot via the configured store; always keep `contextDelta` when `includeDelta = true`.
- **预估复杂度**: M

#### Task 6.4: Trace archive manifest
- **文件**: Create `src/observability/archive.ts`; modify `src/observability/index.ts`, `src/index.ts`
- **导出接口**: `TraceArchiveManifest`, `ArchiveChunk`, `ArchiveOptions`, `archiveRun`, `validateArchiveManifest`
- **测试**: `tests/observability/archive.test.ts`
- **依赖**: Task 6.2, Task 6.3
- **可并行**: No
- **描述**: Produce cold-storage archive metadata for completed runs.
- **关键实现点**: `archiveRun(runId, options)` reads all traces for the run, computes root trace IDs, loop version map, content-addressed snapshot hashes, writes chunk files with `compression = "none"` for MVP and extension points for gzip/zstd, and returns a manifest with schema version, chunk hashes, and record counts; `validateArchiveManifest()` recomputes hashes and record counts.
- **预估复杂度**: M

#### Task 6.5: Observability end-to-end example
- **文件**: Create `src/examples/trace-query.ts`
- **导出接口**: `makeTraceQueryExample`, `runTraceQueryExample`
- **测试**: `tests/integration/trace-query.example.test.ts`
- **依赖**: Task 6.4
- **可并行**: No
- **描述**: Demonstrate building a nested/forked Trace tree, persisting it to JSONL, querying summaries, and archiving.
- **关键实现点**: Run a small `chain -> fork -> nest` example, persist to `JsonlTraceStore`, create a new store instance from the same path, verify `tree`, `path`, `summarize`, `query({ text })`, and `archiveRun()` all return consistent IDs and counts.
- **预估复杂度**: S

## 并行执行标记汇总

- After Task 0.1, Task 0.2 is the only blocker.
- After Task 0.3, Task 0.4 and Task 0.5 can run in parallel.
- After Phase 0, Task 1.1 and Task 1.4 can run in parallel.
- After Task 2.1, Task 2.2 and Task 2.3 can run in parallel.
- After Phase 0, Task 3.1 can start while Phase 1/2 examples are being completed, but Task 3.2 still depends on Task 1.5 and Task 2.1.
- After Phase 1, Task 4.1, Task 4.3, and Task 4.4 can run in parallel; Task 4.2 depends on Task 4.1.
- After Phase 4, Task 5.1 and Task 5.2 can run in parallel.
- In Phase 6, Task 6.1 and Task 6.2 can run in parallel; Task 6.3 depends on persistent store behavior from Task 6.2.

## 关键路径分析

1. Task 0.1 -> Task 0.2 -> Task 0.3: all later contracts depend on project scaffolding and core serializable primitives.
2. Task 0.4 + Task 0.5 -> Task 0.6: runtime cannot be implemented before loop contracts and Trace storage exist.
3. Task 1.1 -> Task 1.2 -> Task 1.3 -> Task 1.5: `project/emit/merge` depends on immutable patches and is required by `nest` and `fork`.
4. Task 2.1 -> Task 2.2/2.3: composition Trace helpers block both `chain` and `nest`.
5. Task 3.1 -> Task 3.2 -> Task 3.3: fork requires bounded scheduler before safe parallel semantics and quorum can be added.
6. Task 4.1 -> Task 4.2 -> Task 4.5: meta Level 1 depends on mutation contracts and versioned registry transactions.
7. Task 5.1 + Task 5.2 -> Task 5.3 -> Task 5.4: Level 2/3 evolution cannot commit safely before implementation refs, graph validation, and shadow evaluation exist.
8. Task 6.2 -> Task 6.3 -> Task 6.4: sampling and archive depend on JSONL record format and content-ref behavior.

## 集成测试计划

- `tests/integration/minimal-loop.lifecycle.test.ts`: create a loop, run it to completion, assert Context immutability, Trace count, `RunMetrics`, and public barrel imports.
- `tests/integration/context-boundary.lifecycle.test.ts`: parent Context with full affordances projects child Context, child emits observations and knowledge candidates, merge appends state and conditionally accepts knowledge.
- `tests/integration/chain-nest-fork.workflow.test.ts`: run a workflow where `chain(plan, fork(review), nest(verify))` preserves Context flow and reconstructable Trace hierarchy.
- `tests/integration/error-propagation.workflow.test.ts`: verify chain fail-fast, nest propagate vs record-and-continue, fork collect-errors, fork quorum failure, and cancellation traces.
- `tests/integration/meta-level1.workflow.test.ts`: repeated gaps produce a Level 1 ContextMutation, evaluator accepts it, registry active version changes, and subsequent run reads added heuristic.
- `tests/integration/meta-level2-level3.workflow.test.ts`: repeated surprises and timeouts produce Level 2/3 candidates; invalid implementation refs and graph regressions roll back cleanly.
- `tests/integration/observability-persistence.workflow.test.ts`: run nested/forked workflow into JSONL, restart store, query by root, path, fork metadata, summary text, sample snapshots, and archive manifest validation.

## Phase Completion Gates

- Every phase must pass `npm run typecheck`, `npm run lint`, and `npm test`.
- Public API additions must be exported through the nearest module barrel and `src/index.ts`.
- No task may mutate existing Context, Trace, MinimalLoopDefinition, or LoopHandle objects in place.
- All expected runtime failures must return `Result.err`; only invariant violations may throw in development/test mode.
- Every boundary that changes Context scope must produce observable Trace data.
