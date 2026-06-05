import { describe, expect, it } from "vitest";

import {
  asStepNumber,
  createInMemoryTraceSink,
  err,
  InMemoryTraceStore,
  makeLoomError,
  newLoopId,
  newLoopVersion,
  newRunId,
  newTraceId,
  ok,
  type DurationMs,
  type ISODateTime,
  type LoopId,
  type RunId,
  type Trace,
  type TraceEvent,
} from "../../src/index.js";

const now = "2026-06-04T00:00:00.000Z" as ISODateTime;
const oneMs = 1 as DurationMs;

async function collect(query: AsyncIterable<Trace>): Promise<readonly Trace[]> {
  const traces: Trace[] = [];
  for await (const trace of query) {
    traces.push(trace);
  }
  return traces;
}

function makeTrace(
  overrides: Partial<Trace> & {
    readonly runId: RunId;
    readonly loopId: LoopId;
  },
): Trace {
  const id = overrides.id ?? newTraceId();
  return {
    id,
    runId: overrides.runId,
    loopId: overrides.loopId,
    loopVersion: overrides.loopVersion ?? newLoopVersion(),
    stepNumber: overrides.stepNumber ?? asStepNumber(0),
    parentTraceId: overrides.parentTraceId,
    rootTraceId: overrides.rootTraceId ?? id,
    startedAt: overrides.startedAt ?? now,
    endedAt: overrides.endedAt ?? now,
    durationMs: overrides.durationMs ?? oneMs,
    inputContextId: overrides.inputContextId,
    outputContextId: overrides.outputContextId,
    inputSnapshot: overrides.inputSnapshot,
    outputSnapshot: overrides.outputSnapshot,
    outcome: overrides.outcome ?? "pass",
    error: overrides.error,
    observations: overrides.observations ?? [],
    decisions: overrides.decisions ?? [],
    actions: overrides.actions ?? [],
    children: overrides.children ?? [],
    tags: overrides.tags ?? [],
    metadata: overrides.metadata,
  };
}

describe("InMemoryTraceStore", () => {
  it("appends immutable traces and retrieves them by id", async () => {
    const store = new InMemoryTraceStore();
    const trace = makeTrace({ runId: newRunId(), loopId: newLoopId() });

    await expect(store.append(trace)).resolves.toEqual(ok(undefined));
    const result = await store.get(trace.id);

    expect(result).toEqual(ok(trace));
    expect(Object.isFrozen(result.ok ? result.value : undefined)).toBe(true);
  });

  it("queries by run, loop, root, parent, outcome, and tags", async () => {
    const store = new InMemoryTraceStore();
    const runId = newRunId();
    const loopId = newLoopId();
    const otherLoopId = newLoopId();
    const parent = makeTrace({
      runId,
      loopId,
      tags: ["root", "shared"],
    });
    const child = makeTrace({
      runId,
      loopId,
      parentTraceId: parent.id,
      rootTraceId: parent.id,
      outcome: "fail",
      tags: ["child", "shared"],
      error: makeLoomError({
        code: "LOOP_FAILED",
        message: "failed",
        retryable: false,
      }),
    });
    const sibling = makeTrace({
      runId,
      loopId: otherLoopId,
      rootTraceId: parent.id,
      outcome: "cancelled",
      tags: ["sibling"],
    });

    await store.append(parent);
    await store.append(child);
    await store.append(sibling);

    await expect(ids(store.query({ runId }))).resolves.toEqual([
      parent.id,
      child.id,
      sibling.id,
    ]);
    await expect(ids(store.query({ loopId }))).resolves.toEqual([parent.id, child.id]);
    await expect(ids(store.query({ rootTraceId: parent.id }))).resolves.toEqual([
      parent.id,
      child.id,
      sibling.id,
    ]);
    await expect(ids(store.query({ parentTraceId: parent.id }))).resolves.toEqual([
      child.id,
    ]);
    await expect(ids(store.query({ outcome: ["fail"] }))).resolves.toEqual([child.id]);
    await expect(ids(store.query({ tags: ["shared"] }))).resolves.toEqual([
      parent.id,
      child.id,
    ]);
  });

  it("returns child traces by parentTraceId", async () => {
    const store = new InMemoryTraceStore();
    const runId = newRunId();
    const loopId = newLoopId();
    const parent = makeTrace({ runId, loopId });
    const child = makeTrace({
      runId,
      loopId,
      parentTraceId: parent.id,
      rootTraceId: parent.id,
    });

    await store.append(parent);
    await store.append(child);

    await expect(ids(store.children(parent.id))).resolves.toEqual([child.id]);
  });

  it("records streaming events and persists step.completed traces", async () => {
    const store = new InMemoryTraceStore();
    const trace = makeTrace({ runId: newRunId(), loopId: newLoopId() });
    const started: TraceEvent = {
      type: "step.started",
      traceId: trace.id,
      at: now,
      contextId: trace.inputContextId,
    };
    const completed: TraceEvent = {
      type: "step.completed",
      trace,
      at: now,
    };

    await store.appendEvent(started);
    await store.appendEvent(completed);

    expect(store.events()).toEqual([started, completed]);
    await expect(store.get(trace.id)).resolves.toEqual(ok(trace));
  });

  it("creates a sink that writes events to the store", async () => {
    const store = new InMemoryTraceStore();
    const sink = createInMemoryTraceSink(store);
    const trace = makeTrace({ runId: newRunId(), loopId: newLoopId() });

    await expect(
      sink.emit({
        type: "step.completed",
        trace,
        at: now,
      }),
    ).resolves.toEqual(ok(undefined));

    await expect(store.get(trace.id)).resolves.toEqual(ok(trace));
  });

  it("returns LoomError results for missing traces", async () => {
    const store = new InMemoryTraceStore();
    const missingId = newTraceId();
    const result = await store.get(missingId);

    expect(result).toEqual(
      err(
        makeLoomError({
          code: "VALIDATION_FAILED",
          message: "Trace not found",
          retryable: false,
          traceId: missingId,
        }),
      ),
    );
  });
});

async function ids(query: AsyncIterable<Trace>): Promise<readonly string[]> {
  const traces = await collect(query);
  return traces.map((trace) => trace.id);
}
