export interface ComposedAbort {
  readonly signal: AbortSignal;
  readonly isTimeout: () => boolean;
  cleanup(): void;
}

export function composeAbort(signal?: AbortSignal, timeoutMs?: number): ComposedAbort {
  const controller = new AbortController();
  let timeoutHit = false;
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  const abortFromParent = (): void => {
    controller.abort(signal?.reason);
  };

  if (signal?.aborted === true) {
    abortFromParent();
  } else if (signal !== undefined) {
    signal.addEventListener("abort", abortFromParent, { once: true });
  }

  if (timeoutMs !== undefined) {
    timeoutId = setTimeout(() => {
      timeoutHit = true;
      controller.abort(new Error("Timeout"));
    }, timeoutMs);
  }

  return {
    signal: controller.signal,
    isTimeout: () => timeoutHit,
    cleanup: () => {
      if (timeoutId !== undefined) {
        clearTimeout(timeoutId);
      }
      signal?.removeEventListener("abort", abortFromParent);
    },
  };
}
