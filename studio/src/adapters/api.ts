/**
 * The one place Studio talks to the backend.
 *
 * Adapters call the APIs that already exist. Nothing here duplicates a
 * store or an endpoint: projects, sessions and approvals stay authoritative
 * on the server, and this layer only shapes them for the screens
 * (DECISIONES_UI.md, "no duplicar APIs o stores autoritativos").
 */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    signal,
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });

  if (!response.ok) {
    throw new ApiError(`${path} responded ${response.status}`, response.status);
  }

  return (await response.json()) as T;
}

/**
 * Several endpoints return a bare array and others wrap it. Screens should
 * not care which, and should certainly not crash because one of them
 * changed shape.
 */
export function asArray<T>(value: unknown, key?: string): T[] {
  if (Array.isArray(value)) return value as T[];
  if (value && typeof value === 'object' && key) {
    const inner = (value as Record<string, unknown>)[key];
    if (Array.isArray(inner)) return inner as T[];
  }
  return [];
}
