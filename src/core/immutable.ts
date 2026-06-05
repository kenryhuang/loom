import type { JsonPrimitive } from "./json.js";

export type Immutable<T> = T extends (...args: readonly never[]) => unknown
  ? T
  : T extends JsonPrimitive
    ? T
    : T extends readonly (infer U)[]
      ? readonly Immutable<U>[]
      : T extends Map<infer K, infer V>
        ? ReadonlyMap<Immutable<K>, Immutable<V>>
        : T extends Set<infer U>
          ? ReadonlySet<Immutable<U>>
          : { readonly [K in keyof T]: Immutable<T[K]> };

export type MaybePromise<T> = T | Promise<T>;

export function deepFreeze<T>(value: T): Immutable<T> {
  if (value === null || typeof value !== "object") {
    return value as Immutable<T>;
  }

  if (Object.isFrozen(value)) {
    return value as Immutable<T>;
  }

  for (const key of Reflect.ownKeys(value)) {
    const child = (value as Readonly<Record<PropertyKey, unknown>>)[key];
    deepFreeze(child);
  }

  return Object.freeze(value) as Immutable<T>;
}
