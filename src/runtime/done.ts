import { makeLoomError, toLoomError } from "../core/errors.js";
import type { AnyContext } from "../core/context.js";
import type { DoneOptions, LoopHandle, RuntimeRegistry } from "../core/loop.js";
import { err, ok, type Result } from "../core/result.js";
import { composeAbort } from "./cancellation.js";
import { defaultRuntimeRegistry, getRuntimeState } from "./registry.js";
import type { ISODateTime } from "../core/ids.js";

export async function done<TContext extends AnyContext>(
  loop: LoopHandle<TContext>,
  context: TContext,
  options: DoneOptions = {},
): Promise<Result<boolean>> {
  const abort = composeAbort(options.signal, options.timeoutMs);
  const registry = getRuntimeState(loop)?.registry ?? defaultRuntimeRegistry;

  try {
    if (abort.signal.aborted) {
      return err(
        makeLoomError({
          code: abort.isTimeout() ? "TIMEOUT" : "ABORTED",
          message: abort.isTimeout() ? "Operation timed out" : "Operation aborted",
          retryable: abort.isTimeout(),
        }),
      );
    }

    if (isBudgetExhausted(context)) {
      return ok(true);
    }

    const criteriaDone = await evaluateRequiredCriteria(context, registry, options);
    if (!criteriaDone.ok || criteriaDone.value) {
      return criteriaDone;
    }

    const result = await loop.definition.done(context, {
      signal: abort.signal,
      now,
      registry,
    });

    return result;
  } catch (error) {
    return err(toLoomError(error));
  } finally {
    abort.cleanup();
  }
}

function isBudgetExhausted(context: AnyContext): boolean {
  const maxSteps = context.goal.budget.maxSteps;
  return maxSteps !== undefined && context.state.observations.length >= maxSteps;
}

async function evaluateRequiredCriteria(
  context: AnyContext,
  registry: RuntimeRegistry,
  options: DoneOptions,
): Promise<Result<boolean>> {
  const criteria = context.goal.criteria.filter(
    (criterion) => criterion.required && criterion.evaluator !== undefined,
  );
  if (criteria.length === 0) {
    return ok(false);
  }

  for (const criterion of criteria) {
    if (criterion.evaluator === undefined) {
      return ok(false);
    }

    const evaluator = registry.evaluators.get(criterion.evaluator);
    if (!evaluator.ok) {
      return evaluator;
    }

    const passed = await evaluator.value.evaluate(context, criterion, options);
    if (!passed.ok) {
      return passed;
    }
    if (!passed.value) {
      return ok(false);
    }
  }

  return ok(true);
}

function now(): ISODateTime {
  return new Date().toISOString() as ISODateTime;
}
