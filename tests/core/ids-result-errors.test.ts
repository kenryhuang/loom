import { describe, expect, it } from "vitest";

import {
  asStepNumber,
  err,
  isErr,
  isOk,
  makeLoomError,
  newContextId,
  newLoopId,
  newLoopVersion,
  newRunId,
  newTraceId,
  ok,
  toLoomError,
  type LoomError,
  type Metadata,
  type Result,
} from "../../src/index.js";

describe("branded ids", () => {
  it("creates prefixed ids and starts loop versions at v1", () => {
    expect(newLoopId()).toMatch(/^loop_/u);
    expect(newContextId()).toMatch(/^ctx_/u);
    expect(newTraceId()).toMatch(/^trace_/u);
    expect(newRunId()).toMatch(/^run_/u);
    expect(newLoopVersion()).toBe("v1");
  });

  it("rejects negative or unsafe step numbers", () => {
    expect(asStepNumber(0)).toBe(0);
    expect(asStepNumber(3)).toBe(3);
    expect(() => asStepNumber(-1)).toThrow("StepNumber");
    expect(() => asStepNumber(1.5)).toThrow("StepNumber");
  });
});

describe("Result", () => {
  it("narrows ok and err variants", () => {
    const success: Result<number> = ok(42);
    const failure: Result<number> = err(
      makeLoomError({
        code: "VALIDATION_FAILED",
        message: "bad input",
        retryable: false,
      }),
    );

    expect(isOk(success)).toBe(true);
    expect(isErr(success)).toBe(false);
    expect(isErr(failure)).toBe(true);
    expect(isOk(failure)).toBe(false);
  });
});

describe("LoomError", () => {
  it("preserves LoomError values", () => {
    const original = makeLoomError({
      code: "TOOL_FAILED",
      message: "tool failed",
      retryable: true,
      metadata: { toolId: "search" },
    });

    expect(toLoomError(original)).toBe(original);
  });

  it("maps ordinary exceptions to non-retryable INTERNAL errors", () => {
    const mapped = toLoomError(new Error("boom"));

    expect(mapped.code).toBe("INTERNAL");
    expect(mapped.message).toBe("boom");
    expect(mapped.retryable).toBe(false);
    expect(mapped.cause).toEqual({ name: "Error", message: "boom" });
  });

  it("keeps metadata JSON compatible", () => {
    const metadata: Metadata = {
      tags: ["phase-0", "core"],
      nested: { count: 1, active: true, empty: null },
    };
    const error: LoomError = makeLoomError({
      code: "INTERNAL",
      message: "internal",
      retryable: false,
      metadata,
    });

    expect(error.metadata).toEqual(metadata);
  });
});
