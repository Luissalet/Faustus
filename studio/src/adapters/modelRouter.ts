import { ApiError, responseReason } from './api';

/**
 * OBJ-8 / Lote B1 (contract in scratchpad/CONTRATO_OBJ8_B.md) — MOD-05's
 * admin surface, `/api/model-router/*` (backend: `src/model_router.py` +
 * `routes/model_router_routes.py`, see `docs/api/model_router.md`). Chooses
 * which LOCAL model answers a turn from evidence this installation has
 * actually recorded — never from a fixed name list — and **never** picks a
 * paid model itself: `escalated: true` only ever means "the caller must
 * decide", read `Decision.escalation` and act, or don't.
 *
 * Admin-only, same gate as `adapters/openrouter.ts`. This adapter only
 * shapes the calls and surfaces what the server said no to.
 */

/** `src/model_router.py::VALID_CAPABILITIES`. */
export type ModelRouterCapability = 'tool_call' | 'json_mode' | 'vision' | 'reasoning';
export const MODEL_ROUTER_CAPABILITIES: ModelRouterCapability[] = ['tool_call', 'json_mode', 'vision', 'reasoning'];

export interface ModelRouterConfig {
  enabled: boolean;
  prefer_local: boolean;
  max_latency_s: number | null;
  allow_paid_escalation: boolean;
  candidates: string[];
  min_capabilities: ModelRouterCapability[];
}

/** `RouterConfig()`'s inert defaults — `enabled=false` means nothing about
 *  today's behaviour changes until an admin turns this on explicitly. */
export const DEFAULT_MODEL_ROUTER_CONFIG: ModelRouterConfig = {
  enabled: false,
  prefer_local: true,
  max_latency_s: null,
  allow_paid_escalation: false,
  candidates: [],
  min_capabilities: [],
};

export interface ModelRouterRequirements {
  capabilities: ModelRouterCapability[];
  max_latency_s: number | null;
}

export interface ModelRouterScored {
  model: string;
  score: number;
  why: string[];
  meets_requirements: boolean;
}

export interface ModelRouterDecision {
  /** `null` means no local model was chosen THIS call — never a paid one;
   *  a paid completion needs the caller to read `escalated`/`escalation`
   *  and act on it deliberately. */
  model: string | null;
  reason: string;
  alternatives: ModelRouterScored[];
  escalated: boolean;
  escalation: Record<string, unknown> | null;
}

export interface ModelRouterPreviewResult {
  decision: ModelRouterDecision;
  explain: string;
}

export interface ModelRouterLogEntry {
  ts?: string;
  session_id?: string;
  requested?: string;
  chosen?: string | null;
  reason?: string;
  escalated?: boolean;
  candidates?: ModelRouterScored[];
  [key: string]: unknown;
}

export interface ModelRouterStatsEntry {
  ok: number;
  fail: number;
  ewma_latency_s: number | null;
  last_error_class: string | null;
  updated_at: string;
}

/** Same shape as `OpenRouterApiError`/`GitApiError`/`BoardApiError`:
 *  `error_class` (`model_router.invalid_config`) alongside the message. */
export class ModelRouterApiError extends ApiError {
  readonly errorClass: string | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'ModelRouterApiError';
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
    throw new ModelRouterApiError(await responseReason(response, path), response.status, errorClass, payload);
  }
  return (await response.json()) as T;
}

/** `GET /api/model-router/config`. */
export function getModelRouterConfig(): Promise<ModelRouterConfig> {
  return request('/api/model-router/config');
}

/** `PUT /api/model-router/config` — patch, only the given keys change. */
export function updateModelRouterConfig(patch: Partial<ModelRouterConfig>): Promise<ModelRouterConfig> {
  return request('/api/model-router/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch ?? {}),
  });
}

/** `POST /api/model-router/preview` — runs `choose()` with `log=False`;
 *  never appears in the Registro tab. */
export function previewModelRouterDecision(
  requirements: Partial<ModelRouterRequirements>,
  installed: string[],
): Promise<ModelRouterPreviewResult> {
  return request('/api/model-router/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requirements: requirements ?? {}, installed: installed ?? [] }),
  });
}

/** `GET /api/model-router/log?limit=` — most recent decisions, newest first. */
export function getModelRouterLog(limit = 50): Promise<{ entries: ModelRouterLogEntry[] }> {
  const n = Math.max(1, Math.min(Math.trunc(limit) || 50, 2000));
  return request(`/api/model-router/log?limit=${n}`);
}

/** `GET /api/model-router/stats` — ok/fail/latency history per model. */
export function getModelRouterStats(): Promise<{ stats: Record<string, ModelRouterStatsEntry> }> {
  return request('/api/model-router/stats');
}

/* ────────────────────────── Pure helpers ──────────────────────────
 * No DOM, no fetch: exercised directly by
 * `studio/checks/openrouter_router.check.mjs`. */

function samePrimitive(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** The minimal PUT body for going from `base` to `draft` — same "only what
 *  changed" contract `diffOpenRouterPrefs` follows for the sibling adapter. */
export function diffModelRouterConfig(base: ModelRouterConfig, draft: ModelRouterConfig): Partial<ModelRouterConfig> {
  const patch: Partial<ModelRouterConfig> = {};
  if (draft.enabled !== base.enabled) patch.enabled = draft.enabled;
  if (draft.prefer_local !== base.prefer_local) patch.prefer_local = draft.prefer_local;
  if (draft.max_latency_s !== base.max_latency_s) patch.max_latency_s = draft.max_latency_s;
  if (draft.allow_paid_escalation !== base.allow_paid_escalation) patch.allow_paid_escalation = draft.allow_paid_escalation;
  if (!samePrimitive(draft.candidates, base.candidates)) patch.candidates = draft.candidates;
  if (!samePrimitive(draft.min_capabilities, base.min_capabilities)) patch.min_capabilities = draft.min_capabilities;
  return patch;
}

export function modelRouterConfigDirty(base: ModelRouterConfig, draft: ModelRouterConfig): boolean {
  return Object.keys(diffModelRouterConfig(base, draft)).length > 0;
}

/** Comma/newline-separated text -> a trimmed, deduplicated model-name list —
 *  same convention `Settings.tsx`'s "Utility fallbacks" field already uses
 *  (`fromList` in `screens/settings/fields.tsx`), kept local here so this
 *  adapter stays free of a screens-layer import. */
export function modelListFromText(s: string): string[] {
  const out: string[] = [];
  for (const raw of s.split(/[,\n]/)) {
    const name = raw.trim();
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

export function modelListToText(list: string[]): string {
  return list.join(', ');
}

/**
 * A one-line, translated-by-the-caller-free summary of a decision, for a
 * place too small for the full alternatives table (a toast, a log row
 * tooltip). Pure so it is testable without a screen.
 */
export function summarizeDecision(decision: ModelRouterDecision): string {
  if (decision.model) return decision.model;
  if (decision.escalated) return 'escalated';
  return 'none';
}
