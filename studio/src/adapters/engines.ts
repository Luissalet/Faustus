import { getJson } from './api';

/**
 * Local inference engines (Settings → Local models → Engines): `llama-
 * server` instances the person configures and starts/stops from the UI
 * instead of an out-of-band script (`routes/engine_routes.py`, built on
 * `src/engines.py`/`src/launch_profiles.py`). Same `call<T>` fetch shape
 * `adapters/localModels.ts` already uses for this screen's other calls.
 */

const API = '/api/engines';
const JSON_HEADERS = { 'Content-Type': 'application/json' };

export interface EngineConfig {
  id: string;
  name: string;
  executable: string;
  model_path: string;
  ctx_size: number;
  host: string;
  port: number | null;
  extra_args: string[];
  mtp: boolean;
  mtp_draft_n_max: number;
  /** From the GGUF's own metadata (`*.nextn_predict_layers`): true when the
   * model ships MTP/nextn draft-head layers, false when it doesn't, null
   * when unreadable/unknown (never blocks — same "ignorance never blocks"
   * rule as the VRAM admission check). */
  mtp_supported: boolean | null;
  /** Current `-np`/`--parallel` value, if the engine's extra flags set one. */
  parallel: number | null;
  description?: string | null;
}

export interface EngineStatus {
  state: 'running' | 'unhealthy' | 'stopped' | 'unknown';
  root: string;
  pid: number | null;
  pid_command: string | null;
  model: string;
  context_length: number;
  footprint_bytes: number | null;
  footprint_measured: boolean;
  generating: boolean;
  configured_model_path: string;
  configured_ctx_size: number;
  error?: string;
}

export interface EngineCreateInput {
  name: string;
  executable: string;
  model_path: string;
  ctx_size: number;
  port: number;
  host?: string;
  extra_args?: string[];
  mtp?: boolean;
  mtp_draft_n_max?: number;
  description?: string;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init });
  const text = await r.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    /* not json */
  }
  if (!r.ok) {
    const d = data as { detail?: unknown; error?: string };
    throw new Error(typeof d.detail === 'string' ? d.detail : d.error ?? `HTTP ${r.status}`);
  }
  return data as T;
}

export const listEngines = () => getJson<EngineConfig[]>(API);
export const engineStatuses = () => getJson<Record<string, EngineStatus>>(`${API}/status`);
export const engineStatus = (id: string) => getJson<EngineStatus>(`${API}/${encodeURIComponent(id)}/status`);
export const createEngine = (body: EngineCreateInput) =>
  call<EngineConfig>(API, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) });
export const updateEngine = (id: string, patch: Partial<EngineCreateInput>) =>
  call<EngineConfig>(`${API}/${encodeURIComponent(id)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(patch) });
export const deleteEngine = (id: string) => call<{ deleted: boolean }>(`${API}/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const startEngine = (id: string) => call<Record<string, unknown>>(`${API}/${encodeURIComponent(id)}/start`, { method: 'POST' });
export const stopEngine = (id: string) => call<Record<string, unknown>>(`${API}/${encodeURIComponent(id)}/stop`, { method: 'POST' });
export const verifyEngine = (id: string) => call<Record<string, unknown>>(`${API}/${encodeURIComponent(id)}/verify`, { method: 'POST' });
export const discoverEngine = (port: number, host = '127.0.0.1') =>
  getJson<{ found: boolean; model_path?: string; ctx_size?: number }>(`${API}/discover?port=${port}&host=${encodeURIComponent(host)}`);
