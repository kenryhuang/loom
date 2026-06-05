import type { TokenUsage } from "./provider.js";

export interface TokenTracker {
  add(usage: TokenUsage): void;
  readonly total: TokenUsage;
  isWithinBudget(maxTokens?: number): boolean;
  reset(): void;
}

export function createTokenTracker(): TokenTracker {
  let total: TokenUsage = zeroUsage();

  return {
    add: (usage) => {
      total = {
        promptTokens: total.promptTokens + usage.promptTokens,
        completionTokens: total.completionTokens + usage.completionTokens,
        totalTokens: total.totalTokens + usage.totalTokens,
      };
    },
    get total() {
      return { ...total };
    },
    isWithinBudget: (maxTokens) => {
      return maxTokens === undefined || total.totalTokens <= maxTokens;
    },
    reset: () => {
      total = zeroUsage();
    },
  };
}

function zeroUsage(): TokenUsage {
  return {
    promptTokens: 0,
    completionTokens: 0,
    totalTokens: 0,
  };
}
