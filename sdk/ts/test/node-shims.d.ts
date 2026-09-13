/**
 * Minimal ambient types for the two Node built-ins the test suite uses.
 * This package has zero dependencies (CONTRATO_SDK_S2.md § S2.1: "cero
 * dependencias de runtime y cero peers"), including no `@types/node` dev
 * dependency — so `node:test`/`node:assert/strict` need a hand-written
 * shim instead. Only the surface these tests actually call.
 */
declare module 'node:test' {
  export function test(name: string, fn: () => void | Promise<void>): void;
}

declare module 'node:assert/strict' {
  interface StrictAssert {
    (value: unknown, message?: string | Error): asserts value;
    ok(value: unknown, message?: string | Error): asserts value;
    equal(actual: unknown, expected: unknown, message?: string | Error): void;
    deepEqual(actual: unknown, expected: unknown, message?: string | Error): void;
    rejects(
      block: (() => Promise<unknown>) | Promise<unknown>,
      error?: RegExp | ((err: unknown) => boolean) | Record<string, unknown> | { new (...args: never[]): unknown },
      message?: string,
    ): Promise<void>;
  }
  const assert: StrictAssert;
  export default assert;
}
