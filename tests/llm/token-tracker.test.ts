import { describe, expect, it } from "vitest";

import { createTokenTracker } from "../../src/index.js";

describe("token tracker", () => {
  it("starts at zero and accumulates token usage", () => {
    const tracker = createTokenTracker();

    expect(tracker.total).toEqual({
      promptTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
    });

    tracker.add({ promptTokens: 10, completionTokens: 5, totalTokens: 15 });
    tracker.add({ promptTokens: 2, completionTokens: 3, totalTokens: 5 });

    expect(tracker.total).toEqual({
      promptTokens: 12,
      completionTokens: 8,
      totalTokens: 20,
    });
  });

  it("checks optional token budgets and can reset", () => {
    const tracker = createTokenTracker();

    tracker.add({ promptTokens: 8, completionTokens: 4, totalTokens: 12 });

    expect(tracker.isWithinBudget()).toBe(true);
    expect(tracker.isWithinBudget(12)).toBe(true);
    expect(tracker.isWithinBudget(11)).toBe(false);

    tracker.reset();

    expect(tracker.total.totalTokens).toBe(0);
    expect(tracker.isWithinBudget(0)).toBe(true);
  });
});
