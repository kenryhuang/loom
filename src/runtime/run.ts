import { makeLoomError } from "../core/errors.js";
import type { AnyContext, Observation } from "../core/context.js";
import type { JsonValue } from "../core/json.js";
import type { LoopHandle, RunOptions, RunResult } from "../core/loop.js";
import { err, ok, type Result } from "../core/result.js";
import type { DurationMs, ISODateTime } from "../core/ids.js";
import type { Trace } from "../core/trace.js";
import { done } from "./done.js";
import { step } from "./step.js";

export async function run<
  TContext extends AnyContext,
  TObservation extends Observation,
  TOutput extends JsonValue,
>(
  loop: LoopHandle<TContext, TObservation, TOutput>,
  initialContext: TContext,
  options: RunOptions = {},
): Promise<Result<RunResult<TContext, TOutput>>> {
  const startedAtMs = Date.now();
  const startedAt = now();
  let current = initialContext;
  let steps = 0;
  let output: TOutput | undefined;
  const traces: Trace[] = [];

  for (;;) {
    const isDone = await done(loop, current, options);
    if (!isDone.ok) {
      return err(isDone.error);
    }
    if (isDone.value) {
      return ok({
        context: current,
        traces,
        ...(output === undefined ? {} : { output }),
        metrics: {
          steps,
          startedAt,
          endedAt: now(),
          durationMs: durationSince(startedAtMs),
          traceCount: traces.length,
          outcome: "pass",
        },
      });
    }

    if (options.maxSteps !== undefined && steps >= options.maxSteps) {
      return err(
        makeLoomError({
          code: "BUDGET_EXCEEDED",
          message: "Run maxSteps exceeded",
          retryable: false,
        }),
      );
    }

    const stepped = await step(loop, current, options);
    if (!stepped.ok) {
      return err(stepped.error);
    }

    current = stepped.value.context;
    traces.push(stepped.value.trace);
    output = stepped.value.output;
    steps += 1;
  }
}

function durationSince(startedAtMs: number): DurationMs {
  return Math.max(0, Date.now() - startedAtMs) as DurationMs;
}

function now(): ISODateTime {
  return new Date().toISOString() as ISODateTime;
}
