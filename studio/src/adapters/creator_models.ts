import { ApiError, responseReason } from './api';

/**
 * WP08 — Studio adapter over the Model Explorer API
 * (`routes/creator_model_explorer_routes.py`, over `src/creator/model_explorer.py`).
 *
 * Same posture as `adapters/creator.ts`: every route below answers 404 with
 * `{"detail": "creator is not enabled"}` while `creator_enabled` is off, and
 * this adapter does not special-case it — the screen reads `.status`.
 *
 * `basis` on every cell/footprint field is one of `known | announced |
 * unsupported | unknown | measured | estimated` (`src/creator/
 * model_explorer.py::BASES`) — MOD-11's own rule: a missing field is the
 * literal string `"unknown"`, never converted to zero/false, and an
 * estimated number is never labelled as measured. This adapter passes those
 * strings through verbatim; it does not reinterpret them.
 */

export type CompareBasis = 'known' | 'announced' | 'unsupported' | 'unknown' | 'measured' | 'estimated';

export interface ExplorerRow {
  deployment_id: string;
  model_spec_id: string;
  vendor: string;
  family: string;
  model_id: string;
  license: string;
  context_tokens: number;
  engine: { kind?: string; version?: string; build_digest?: string | null };
  endpoint_id: string;
  known_axes: string[];
  announced_axes: string[];
}

export interface CapabilityAxisEvidence {
  axis: string;
  capability: string;
  status: string;
  level: string;
  source: string;
  observed_at: string;
  conditions: Record<string, unknown>;
  method: string;
}

export interface FootprintBlock {
  size_bytes: number;
  basis: CompareBasis;
  vram_state: string;
  vram_note: string;
  reason: string;
}

export interface ExplorerEntry {
  deployment_id: string;
  model_spec: Record<string, unknown>;
  deployment: Record<string, unknown>;
  capability_profile: { deployment_id: string; model_spec_id: string; axes: Record<string, CapabilityAxisEvidence> };
  param_schemas: Array<{ engine: string; task: string; fields: unknown[] }>;
  footprint: FootprintBlock;
  legacy_calibration: Record<string, unknown> | null;
}

export interface CompareCell {
  value: unknown;
  basis: CompareBasis;
}

export interface CompareRow {
  field: string;
  label: string;
  cells: Record<string, CompareCell>;
}

export interface CompareTable {
  task: string;
  deployment_ids: string[];
  rows: CompareRow[];
  unknown_deployment_ids: string[];
}

export class ModelExplorerApiError extends ApiError {
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'ModelExplorerApiError';
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
    const detail = payload.detail && typeof payload.detail === 'object'
      ? (payload.detail as Record<string, unknown>)
      : payload;
    const message = await responseReason(response, path);
    throw new ModelExplorerApiError(message, response.status, detail);
  }
  return (await response.json()) as T;
}

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
}

// ── explorer ─────────────────────────────────────────────────────────────

export function listExplorer(signal?: AbortSignal): Promise<{ deployments: ExplorerRow[] }> {
  return get('/api/creator/models/explorer', signal);
}

export function getExplorerEntry(deploymentId: string, signal?: AbortSignal): Promise<ExplorerEntry> {
  return get(`/api/creator/models/explorer/${encodeURIComponent(deploymentId)}`, signal);
}

// ── compare (2-3 deployments, one task) ─────────────────────────────────

export function compareDeployments(deploymentIds: string[], task: string): Promise<CompareTable> {
  return post('/api/creator/models/compare', { deployment_ids: deploymentIds, task });
}
