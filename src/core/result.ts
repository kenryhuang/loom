import type { LoomError } from "./errors.js";

export type Result<T, E extends LoomError = LoomError> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: E };

export function ok<T>(value: T): Result<T> {
  return { ok: true, value };
}

export function err<E extends LoomError>(error: E): Result<never, E> {
  return { ok: false, error };
}

export function isOk<T, E extends LoomError>(
  result: Result<T, E>,
): result is { readonly ok: true; readonly value: T } {
  return result.ok;
}

export function isErr<T, E extends LoomError>(
  result: Result<T, E>,
): result is { readonly ok: false; readonly error: E } {
  return !result.ok;
}
