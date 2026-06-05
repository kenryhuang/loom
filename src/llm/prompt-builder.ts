import type { AnyContext, Decision, KnowledgeItem, Observation } from "../core/context.js";
import type { LlmMessage } from "./provider.js";

export interface PromptBuilderOptions {
  readonly includeKnowledge?: boolean;
  readonly includeHistory?: boolean;
  readonly maxHistorySteps?: number;
}

export function buildSystemPrompt(context: AnyContext): string {
  const lines: string[] = [
    `You are the loop brain for this Loom context. Your role is: ${context.identity.role}.`,
    "",
    "Capabilities:",
    ...context.identity.capabilities.map(
      (capability) => `- ${capability.id}: ${capability.description}`,
    ),
    "",
    "Constraints:",
    ...context.identity.constraints.map((constraint) => {
      const prefix = constraint.severity === "must" ? "MUST" : constraint.severity.toUpperCase();
      return `- ${prefix}: ${constraint.description}`;
    }),
    "",
    "Goal:",
    `- Objective: ${context.goal.objective}`,
    "Success criteria:",
    ...context.goal.criteria.map((criterion) => {
      const required = criterion.required ? "required" : "optional";
      return `- ${criterion.id} (${required}): ${criterion.description}`;
    }),
    "",
    "Available tools:",
    ...formatTools(context),
    "",
    "Output format:",
    "Return only valid JSON with this shape:",
    [
      "{",
      '  "reasoning": "why this action is the best next step",',
      '  "action": {',
      '    "kind": "tool" | "none" | "custom",',
      '    "description": "short executable action description",',
      '    "target": "tool id when kind is tool",',
      '    "input": {}',
      "  },",
      '  "alternatives": [],',
      '  "confidence": 0.0',
      "}",
    ].join("\n"),
  ];

  return lines.join("\n");
}

export function buildUserPrompt(context: AnyContext, options: PromptBuilderOptions = {}): string {
  const includeHistory = options.includeHistory === undefined ? true : options.includeHistory;
  const includeKnowledge = options.includeKnowledge === undefined ? true : options.includeKnowledge;
  const maxHistorySteps = options.maxHistorySteps === undefined ? 5 : options.maxHistorySteps;
  const stepNumber = context.state.observations.length;
  const lines: string[] = [
    "Current loop state:",
    `- Context id: ${context.id}`,
    `- Step number: ${String(stepNumber)}`,
    `- Budget: ${formatBudget(context)}`,
  ];

  if (includeHistory) {
    lines.push(
      "",
      "Recent observations:",
      ...formatObservations(takeLast(context.state.observations, maxHistorySteps)),
      "",
      "Recent decisions:",
      ...formatDecisions(takeLast(context.state.decisions, maxHistorySteps)),
    );
  }

  if (includeKnowledge) {
    lines.push(
      "",
      "Knowledge facts:",
      ...formatKnowledge(context.knowledge.facts),
      "",
      "Knowledge heuristics:",
      ...formatKnowledge(context.knowledge.heuristics),
    );
  }

  lines.push("", "Choose the next decision and action using the required JSON format.");
  return lines.join("\n");
}

export function buildMessages(
  context: AnyContext,
  options: PromptBuilderOptions = {},
): readonly LlmMessage[] {
  return [
    {
      role: "system",
      content: buildSystemPrompt(context),
    },
    {
      role: "user",
      content: buildUserPrompt(context, options),
    },
  ];
}

function formatTools(context: AnyContext): readonly string[] {
  if (context.affordances.tools.length === 0) {
    return ["- none"];
  }
  return context.affordances.tools.map((tool) => `- ${tool.id}: ${tool.description}`);
}

function formatBudget(context: AnyContext): string {
  const budget = context.goal.budget;
  const parts = [
    `maxSteps: ${budget.maxSteps === undefined ? "unbounded" : String(budget.maxSteps)}`,
    `maxDurationMs: ${
      budget.maxDurationMs === undefined ? "unbounded" : String(budget.maxDurationMs)
    }`,
    `maxTokens: ${budget.maxTokens === undefined ? "unbounded" : String(budget.maxTokens)}`,
    `maxCostUsd: ${budget.maxCostUsd === undefined ? "unbounded" : String(budget.maxCostUsd)}`,
  ];
  return parts.join(", ");
}

function formatObservations(observations: readonly Observation[]): readonly string[] {
  if (observations.length === 0) {
    return ["- none"];
  }
  return observations.map(
    (observation) =>
      `- ${observation.id} from ${observation.source} at ${observation.at}: ${stringifyJson(
        observation.value,
      )}`,
  );
}

function formatDecisions(decisions: readonly Decision[]): readonly string[] {
  if (decisions.length === 0) {
    return ["- none"];
  }
  return decisions.map(
    (decision) =>
      `- ${decision.id} at ${decision.at}: ${decision.reasoning}; action=${decision.action.kind} ${decision.action.description}`,
  );
}

function formatKnowledge(items: readonly KnowledgeItem[]): readonly string[] {
  if (items.length === 0) {
    return ["- none"];
  }
  return items.map(
    (item) =>
      `- ${item.id} (${item.kind}, confidence ${String(item.confidence)}): ${stringifyJson(
        item.content,
      )}`,
  );
}

function takeLast<T>(items: readonly T[], count: number): readonly T[] {
  if (count <= 0) {
    return [];
  }
  return items.slice(Math.max(0, items.length - count));
}

function stringifyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
