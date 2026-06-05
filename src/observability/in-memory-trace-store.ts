import { deepFreeze } from "../core/immutable.js";
import type { LoopId, RunId, TraceId } from "../core/ids.js";
import { makeLoomError } from "../core/errors.js";
import { err, ok, type Result } from "../core/result.js";
import type { Trace, TraceEvent, TraceOutcome, TraceSink } from "../core/trace.js";
import type { TraceQuery, TraceStore } from "./trace-store.js";

export class InMemoryTraceStore implements TraceStore {
  private readonly traces = new Map<TraceId, Trace>();
  private readonly order: TraceId[] = [];
  private readonly eventsByOrder: TraceEvent[] = [];
  private readonly byRunId = new Map<RunId, TraceId[]>();
  private readonly byLoopId = new Map<LoopId, TraceId[]>();
  private readonly byRootTraceId = new Map<TraceId, TraceId[]>();
  private readonly byParentTraceId = new Map<TraceId, TraceId[]>();
  private readonly byOutcome = new Map<TraceOutcome, TraceId[]>();
  private readonly byTag = new Map<string, TraceId[]>();

  append(trace: Trace): Promise<Result<void>> {
    if (this.traces.has(trace.id)) {
      return Promise.resolve(
        err(
          makeLoomError({
            code: "VALIDATION_FAILED",
            message: "Trace already exists",
            retryable: false,
            traceId: trace.id,
          }),
        ),
      );
    }

    const frozen = deepFreeze(trace) as Trace;
    this.traces.set(frozen.id, frozen);
    this.order.push(frozen.id);
    addIndex(this.byRunId, frozen.runId, frozen.id);
    addIndex(this.byLoopId, frozen.loopId, frozen.id);
    addIndex(this.byRootTraceId, frozen.rootTraceId, frozen.id);
    if (frozen.parentTraceId !== undefined) {
      addIndex(this.byParentTraceId, frozen.parentTraceId, frozen.id);
    }
    addIndex(this.byOutcome, frozen.outcome, frozen.id);
    for (const tag of frozen.tags) {
      addIndex(this.byTag, tag, frozen.id);
    }

    return Promise.resolve(ok(undefined));
  }

  async appendEvent(event: TraceEvent): Promise<Result<void>> {
    const frozen = deepFreeze(event);
    this.eventsByOrder.push(frozen);

    if (frozen.type === "step.completed") {
      return this.append(frozen.trace);
    }

    return ok(undefined);
  }

  get(id: TraceId): Promise<Result<Trace>> {
    const trace = this.traces.get(id);
    if (trace === undefined) {
      return Promise.resolve(
        err(
          makeLoomError({
            code: "VALIDATION_FAILED",
            message: "Trace not found",
            retryable: false,
            traceId: id,
          }),
        ),
      );
    }

    return Promise.resolve(ok(trace));
  }

  async *query(query: TraceQuery): AsyncIterable<Trace> {
    let yielded = 0;
    for (const id of this.candidateIds(query)) {
      await Promise.resolve();
      const trace = this.traces.get(id);
      if (trace !== undefined && matchesTrace(trace, query)) {
        yield trace;
        yielded += 1;
        if (query.limit !== undefined && yielded >= query.limit) {
          return;
        }
      }
    }
  }

  children(id: TraceId): AsyncIterable<Trace> {
    return this.query({ parentTraceId: id });
  }

  events(traceId?: TraceId): readonly TraceEvent[] {
    if (traceId === undefined) {
      return [...this.eventsByOrder];
    }

    return this.eventsByOrder.filter((event) => eventTraceId(event) === traceId);
  }

  private candidateIds(query: TraceQuery): readonly TraceId[] {
    const candidateGroups: readonly TraceId[][] = [
      ...(query.runId === undefined ? [] : [this.byRunId.get(query.runId) ?? []]),
      ...(query.loopId === undefined ? [] : [this.byLoopId.get(query.loopId) ?? []]),
      ...(query.rootTraceId === undefined
        ? []
        : [this.byRootTraceId.get(query.rootTraceId) ?? []]),
      ...(query.parentTraceId === undefined
        ? []
        : [this.byParentTraceId.get(query.parentTraceId) ?? []]),
      ...(query.outcome === undefined
        ? []
        : query.outcome.map((outcome) => this.byOutcome.get(outcome) ?? [])),
      ...(query.tags === undefined ? [] : query.tags.map((tag) => this.byTag.get(tag) ?? [])),
    ];

    if (candidateGroups.length === 0) {
      return [...this.order];
    }

    const [firstGroup, ...remainingGroups] = candidateGroups;
    if (firstGroup === undefined) {
      return [];
    }

    const remainingSets = remainingGroups.map((group) => new Set(group));
    return firstGroup.filter((id) => remainingSets.every((set) => set.has(id)));
  }
}

export function createInMemoryTraceSink(store: TraceStore): TraceSink {
  return {
    emit: (event) => store.appendEvent(event),
  };
}

function addIndex<TKey>(index: Map<TKey, TraceId[]>, key: TKey, traceId: TraceId): void {
  const current = index.get(key);
  if (current === undefined) {
    index.set(key, [traceId]);
    return;
  }

  current.push(traceId);
}

function matchesTrace(trace: Trace, query: TraceQuery): boolean {
  return (
    (query.runId === undefined || trace.runId === query.runId) &&
    (query.loopId === undefined || trace.loopId === query.loopId) &&
    (query.rootTraceId === undefined || trace.rootTraceId === query.rootTraceId) &&
    (query.parentTraceId === undefined || trace.parentTraceId === query.parentTraceId) &&
    (query.outcome === undefined || query.outcome.includes(trace.outcome)) &&
    (query.tags === undefined || query.tags.every((tag) => trace.tags.includes(tag)))
  );
}

function eventTraceId(event: TraceEvent): TraceId {
  return event.type === "step.completed" ? event.trace.id : event.traceId;
}
