import type { Brand, ContextId, ISODateTime, LoopId, LoopVersion, RunId, TraceId } from "./ids.js";
import type { Immutable } from "./immutable.js";
import { deepFreeze } from "./immutable.js";
import type { JsonValue, Metadata } from "./json.js";

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

export type EvaluatorRef = Brand<string, "EvaluatorRef">;
export type ProjectionRef = Brand<string, "ProjectionRef">;
export type ImplementationRef = Brand<string, "ImplementationRef">;

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

export interface Action {
  readonly id: string;
  readonly kind: "tool" | "loop" | "context" | "knowledge" | "none" | "custom";
  readonly description: string;
  readonly input?: JsonValue;
  readonly target?: string;
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

export type AnyContext = Context;

export interface ContextPatch<TContext extends AnyContext = AnyContext> {
  readonly baseContextId: TContext["id"];
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

export function emptyState<TObservation extends Observation = Observation>(): StateLayer<TObservation> {
  return {
    observations: [],
    decisions: [],
    pending: [],
  };
}

export function emptyKnowledge(): KnowledgeLayer {
  return {
    facts: [],
    heuristics: [],
    memories: [],
    version: "v1",
  };
}

export function emptyAffordances(): AffordanceLayer {
  return {
    tools: [],
    loops: [],
    resources: [],
  };
}

export function freezeContext<TContext extends AnyContext>(context: TContext): TContext {
  deepFreeze(context.identity);
  deepFreeze(context.goal);
  deepFreeze(context.state);
  deepFreeze(context.knowledge);
  deepFreeze(context.affordances);
  return deepFreeze(context) as TContext;
}
