import { describe, expect, it } from "vitest";

import {
  create,
  makeInitialCounterContext,
  makeMinimalCounterLoop,
  run,
} from "../../src/index.js";

describe("minimal counter loop example", () => {
  it("runs through the public barrel until the counter budget is reached", async () => {
    const context = makeInitialCounterContext(3);
    const definition = makeMinimalCounterLoop();
    const handle = create(definition);

    expect(handle.ok).toBe(true);
    if (!handle.ok) {
      throw new Error(handle.error.message);
    }

    const result = await run(handle.value, context);

    expect(result.ok).toBe(true);
    if (!result.ok) {
      throw new Error(result.error.message);
    }

    expect(result.value.context.state.observations).toHaveLength(3);
    expect(result.value.context.state.decisions).toHaveLength(3);
    expect(result.value.traces).toHaveLength(3);
    expect(result.value.metrics.steps).toBe(3);
    expect(
      result.value.context.state.observations.map((observation) => observation.value),
    ).toEqual([{ counter: 1 }, { counter: 2 }, { counter: 3 }]);
  });
});
