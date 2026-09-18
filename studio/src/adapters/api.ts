/**
 * The one place Studio talks to the backend.
 *
 * Adapters call the APIs that already exist. Nothing here duplicates a
 * store or an endpoint: projects, sessions and approvals stay authoritative
 * on the server, and this layer only shapes them for the screens
 * (DECISIONES_UI.md, "no duplicar APIs o stores autoritativos").
 */

/**
 * ARCH-01: the wire version this build speaks, and the header names the
 * negotiation runs over (src/api_version.py is the server's half — kept in
 * sync by hand, the same way RUN_ID_HEADER in chat.ts already mirrors
 * X-Odysseus-Run-Id). Sending it costs nothing against an endpoint that
 * doesn't look at it; a server that predates this scheme just ignores an
 * unknown header, exactly like every other additive field in this lot.
 */
export const CLIENT_API_VERSION = '2.0';
export const CLIENT_VERSION_HEADER = 'X-Faustus-Client-Version';
export const API_VERSION_HEADER = 'X-Faustus-Api-Version';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * What went wrong, in the server's own words.
 *
 * The previous interface guessed the cause from the error *text* — if it
 * contained "tool" it announced "this model doesn't support agent tools" —
 * which swallowed every message written specifically to explain a refusal.
 * So: read FastAPI's `{"detail": …}` and show it. When there is nothing to
 * read, say the status and the path and claim nothing else.
 */
export async function responseReason(response: Response, path: string, fallback?: string): Promise<string> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown; message?: unknown };
    const detail = body?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (detail && typeof detail === 'object') {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === 'string' && message.trim()) return message;
    }
    if (typeof body?.message === 'string' && body.message.trim()) return body.message;
  } catch {
    /* not JSON, or already consumed: fall through to the status line */
  }
  return fallback ?? `${path} responded ${response.status}`;
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    signal,
    headers: { Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION },
    credentials: 'same-origin',
  });

  if (!response.ok) {
    throw new ApiError(await responseReason(response, path), response.status);
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
