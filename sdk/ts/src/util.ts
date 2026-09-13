/** Small, defensive readers for untyped wire JSON — no `console.*`, no
 *  substring guessing, just "is this the shape I expect, or nothing". */

export function str(value: unknown): string | undefined {
  return typeof value === 'string' && value.length ? value : undefined;
}

export function num(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

export function bool(value: unknown): boolean {
  return value === true;
}

export function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function arr(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
