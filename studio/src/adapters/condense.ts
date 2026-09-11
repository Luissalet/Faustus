import { ApiError, responseReason } from './api';

/**
 * F3 (CONTRATO_CABLES2) — condense a manually-chosen range of turns into one
 * summary row, a thin typed mirror of `src/condense.py` / `routes/
 * condense_routes.py` (request/response shapes) and `docs/api/condense.md`
 * (the contract this file must never drift from). Field names are kept
 * exactly as the server writes them, same choice `adapters/sideThreads.ts`
 * made — the range rules, the summary prompt and the undo mechanism all
 * stay server-side.
 *
 * Errors: every route here answers a failure with the flat
 * `{"error": str, "error_class": "condense.<motivo>"}` body
 * `routes/condense_routes.py` always returns — the message lives under
 * `error`, not FastAPI's usual `detail`, mirrored from `adapters/
 * sideThreads.ts::request()`.
 */

export interface CondensePreviewTurn {
  index: number;
  role: string;
  excerpt: string;
}

/** `GET .../condense/preview` — no LLM call, safe to run on every start/end
 *  edit in `CondenseDialog`. */
export interface CondensePreview {
  rows: number;
  tokens_before: number;
  tokens_after_estimate: number;
  turns: CondensePreviewTurn[];
}

/** `POST .../condense` — one real call to the session's utility model. */
export interface CondenseResult {
  summary_index: number;
  removed: number;
  tokens_before: number;
  tokens_after: number;
}

/** `POST .../condense/{index}/expand` — restores the range byte-for-byte. */
export interface ExpandResult {
  restored: number;
}

export class CondenseApiError extends ApiError {
  readonly errorClass: string | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'CondenseApiError';
    this.errorClass = errorClass;
    this.payload = payload;
  }
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const errorClass = typeof payload.error_class === 'string' ? payload.error_class : null;
    const flatMessage = typeof payload.error === 'string' && payload.error.trim() ? payload.error : null;
    const message = flatMessage ?? (await responseReason(response, path));
    throw new CondenseApiError(message, response.status, errorClass, payload);
  }
  return (await response.json()) as T;
}

const sessionBase = (id: string) => `/api/session/${encodeURIComponent(id)}`;

/** No LLM call — validates the range and estimates tokens server-side, so
 *  `CondenseDialog` can call this on every start/end edit (debounced) for
 *  free. */
export function previewCondense(sessionId: string, start: number, end: number, signal?: AbortSignal): Promise<CondensePreview> {
  const qs = new URLSearchParams({ start: String(start), end: String(end) });
  return request(`${sessionBase(sessionId)}/condense/preview?${qs.toString()}`, { signal });
}

/** One real call to the session's utility model — collapses
 *  `history[start:end+1]` into a single summary row. */
export function condense(sessionId: string, start: number, end: number): Promise<CondenseResult> {
  return request(`${sessionBase(sessionId)}/condense`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ start, end }),
  });
}

/** Restores the rows a condensed summary at `summaryIndex` replaced,
 *  byte-for-byte (role/content/metadata). No body. */
export function expandCondensed(sessionId: string, summaryIndex: number): Promise<ExpandResult> {
  return request(`${sessionBase(sessionId)}/condense/${encodeURIComponent(String(summaryIndex))}/expand`, {
    method: 'POST',
  });
}
