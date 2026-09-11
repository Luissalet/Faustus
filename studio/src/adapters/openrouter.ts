import { ApiError, responseReason } from './api';

/**
 * OBJ-8 / Lote B1 (contract in scratchpad/CONTRATO_OBJ8_B.md) — per-endpoint
 * OpenRouter option prefs, `/api/openrouter/*` (backend: Lote A2,
 * `src/openrouter_options.py` + `routes/openrouter_routes.py`, see
 * `docs/api/openrouter.md`'s "Opciones por endpoint" section for the full
 * contract this mirrors). Admin-only, same gate as `adapters/commandGuard.ts`
 * and `adapters/git.ts`'s mutations. This adapter only shapes the calls and
 * surfaces what the server said no to — the same split `adapters/git.ts` and
 * `adapters/board.ts` already draw.
 */

export type OpenRouterSort = '' | 'price' | 'throughput' | 'latency';
export type OpenRouterDataCollection = 'auto' | 'allow' | 'deny';

/** `src/openrouter_options.py::SORT_VALUES` / `DATA_COLLECTION_VALUES`. */
export const OPENROUTER_SORT_VALUES: OpenRouterSort[] = ['', 'price', 'throughput', 'latency'];
export const OPENROUTER_DATA_COLLECTION_VALUES: OpenRouterDataCollection[] = ['auto', 'allow', 'deny'];

/** `src/openrouter_options.py::MAX_ORDER_PROVIDERS` / `MIN_`/`MAX_WEB_SEARCH_RESULTS`. */
export const OPENROUTER_MAX_ORDER_PROVIDERS = 20;
export const OPENROUTER_MIN_WEB_SEARCH_RESULTS = 1;
export const OPENROUTER_MAX_WEB_SEARCH_RESULTS = 10;

export interface OpenRouterMaxPrice {
  prompt?: number;
  completion?: number;
}

export interface OpenRouterWebSearch {
  enabled: boolean;
  max_results: number;
}

export interface OpenRouterPrefs {
  sort: OpenRouterSort;
  allow_fallbacks: boolean;
  require_parameters: boolean;
  max_price: OpenRouterMaxPrice | null;
  zdr: boolean;
  order: string[];
  ignore: string[];
  data_collection: OpenRouterDataCollection;
  web_search: OpenRouterWebSearch;
  native_fallback: boolean;
}

/** `src/openrouter_options.py::_default_prefs()`, mirrored so a screen can
 *  render a form before the first GET resolves. */
export const DEFAULT_OPENROUTER_PREFS: OpenRouterPrefs = {
  sort: '',
  allow_fallbacks: true,
  require_parameters: false,
  max_price: null,
  zdr: false,
  order: [],
  ignore: [],
  data_collection: 'auto',
  web_search: { enabled: false, max_results: 5 },
  native_fallback: false,
};

/**
 * A mutation error's whole JSON body, kept alongside the message a plain
 * `ApiError` would show — same shape as `GitApiError`/`BoardApiError`:
 * `error_class` (`openrouter.invalid_prefs`) plus whatever extra the case
 * needs.
 */
export class OpenRouterApiError extends ApiError {
  readonly errorClass: string | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'OpenRouterApiError';
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
    throw new OpenRouterApiError(await responseReason(response, path), response.status, errorClass, payload);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function getOr<T>(path: string): Promise<T> {
  return request<T>(path);
}

function putOr<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
}

function deleteOr<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' });
}

const base = (endpointId: string) => `/api/openrouter/endpoints/${encodeURIComponent(endpointId)}/prefs`;

/** `GET /api/openrouter/prefs` — every endpoint with saved prefs. */
export function getAllOpenRouterPrefs(): Promise<{ endpoints: Record<string, OpenRouterPrefs> }> {
  return getOr('/api/openrouter/prefs');
}

/** `GET /api/openrouter/endpoints/{id}/prefs` — effective prefs (schema
 *  defaults merged with whatever was persisted). Never 404s on an id with
 *  nothing saved — the server always answers the defaults. */
export function getOpenRouterEndpointPrefs(endpointId: string): Promise<OpenRouterPrefs> {
  return getOr(base(endpointId));
}

/** `PUT /api/openrouter/endpoints/{id}/prefs` — partial patch, merged
 *  server-side onto the current (or default) prefs. Returns the resulting
 *  full document. */
export function putOpenRouterEndpointPrefs(endpointId: string, patch: Partial<OpenRouterPrefs>): Promise<OpenRouterPrefs> {
  return putOr(base(endpointId), patch);
}

/** `DELETE /api/openrouter/endpoints/{id}/prefs` — idempotent; the endpoint
 *  falls back to schema defaults. */
export function deleteOpenRouterEndpointPrefs(endpointId: string): Promise<{ deleted: boolean; endpoint_id: string }> {
  return deleteOr(base(endpointId));
}

/* ────────────────────────── Pure helpers ──────────────────────────
 * No DOM, no fetch: exercised directly by
 * `studio/checks/openrouter_router.check.mjs` and reused by the screens. */

/**
 * Whether `baseUrl` is an OpenRouter endpoint — `docs/api/openrouter.md`'s
 * host, `openrouter.ai` (or a subdomain of it, in case a future proxy form
 * shows up), never a substring match against an unrelated host that merely
 * contains the word somewhere in a path or query string.
 */
export function isOpenRouterEndpoint(baseUrl: string): boolean {
  let hostname: string;
  try {
    hostname = new URL(baseUrl).hostname.toLowerCase();
  } catch {
    return false;
  }
  return hostname === 'openrouter.ai' || hostname.endsWith('.openrouter.ai');
}

function samePrimitive(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The minimal PUT body for going from `base` to `draft`: only the fields
 * that actually changed, matching the server's "body = patch parcial"
 * contract — sending a field that did not change risks re-validating (and
 * re-persisting) something nobody touched.
 */
export function diffOpenRouterPrefs(base: OpenRouterPrefs, draft: OpenRouterPrefs): Partial<OpenRouterPrefs> {
  const patch: Partial<OpenRouterPrefs> = {};
  if (draft.sort !== base.sort) patch.sort = draft.sort;
  if (draft.allow_fallbacks !== base.allow_fallbacks) patch.allow_fallbacks = draft.allow_fallbacks;
  if (draft.require_parameters !== base.require_parameters) patch.require_parameters = draft.require_parameters;
  if (!samePrimitive(draft.max_price, base.max_price)) patch.max_price = draft.max_price;
  if (draft.zdr !== base.zdr) patch.zdr = draft.zdr;
  if (!samePrimitive(draft.order, base.order)) patch.order = draft.order;
  if (!samePrimitive(draft.ignore, base.ignore)) patch.ignore = draft.ignore;
  if (draft.data_collection !== base.data_collection) patch.data_collection = draft.data_collection;
  if (!samePrimitive(draft.web_search, base.web_search)) patch.web_search = draft.web_search;
  if (draft.native_fallback !== base.native_fallback) patch.native_fallback = draft.native_fallback;
  return patch;
}

export function openRouterPrefsDirty(base: OpenRouterPrefs, draft: OpenRouterPrefs): boolean {
  return Object.keys(diffOpenRouterPrefs(base, draft)).length > 0;
}

/** Comma/newline-separated text -> a provider-id list, capped the same way
 *  the server caps `order`/`ignore` (`MAX_ORDER_PROVIDERS`) — trims empties
 *  so a trailing comma never becomes a blank provider id. */
export function providerListFromText(s: string): string[] {
  return s
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean)
    .slice(0, OPENROUTER_MAX_ORDER_PROVIDERS);
}

export function providerListToText(list: string[]): string {
  return list.join(', ');
}
