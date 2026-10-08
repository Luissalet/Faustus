import { useCallback, useEffect, useRef, useState } from 'react';
import { CLIENT_API_VERSION, CLIENT_VERSION_HEADER, getJson } from './api';

/**
 * The DGX Spark cluster, through Faustus's proxy to Prometheus's Hoard
 * (routes/sparks_routes.py, src/sparks.py).
 *
 * `GET /api/sparks/status` never fails because of Prometheus: a closed app answers 200 with
 * `{ok: false, error}`. Here that is a state with `ok: false`; the header pill shows nothing for
 * "disabled" and a quiet "Sparks: off" for "unreachable", so the person knows why it is empty.
 */

export interface SparkNode {
  id: string;
  name: string;
  online: boolean;
  error?: string;
  power_state?: string;
  hostname?: string;
  uptime_s?: number;
  memory: { total?: number | null; used?: number | null; percent?: number | null };
  gpu: { util?: number | null; temp_c?: number | null; power_w?: number | null; clock_mhz?: number | null };
  cpu: { percent?: number | null; cores?: number | null };
  fabric: { up: number; total: number; rx_bps: number; tx_bps: number };
  deployments: { recipe: string; title: string; state: string; role: string; served?: string[] }[];
}

export interface SparkDeployment {
  recipe: string;
  title: string;
  state: string;
  step?: string;
  message?: string;
  nodes: string[];
  head: string;
  base_url: string;
  served?: string[];
  served_model_name?: string;
  max_model_len?: number | null;
  external?: boolean;
  context_verified?: boolean | null;
  /** Started outside a recipe (by hand or another tool): offered as an endpoint, not unloaded from here. */
  detected?: boolean;
}

export interface SparkRecipe {
  name: string;
  title: string;
  description?: string;
  nodes?: string[];
  head?: string;
  max_model_len?: number | null;
  memory_gb?: number | null;
  served_model_name?: string;
  tags?: string[];
  invalid?: string;
  validation_status?: string;
  context_verified?: boolean | null;
}

export interface SparksState {
  ok: boolean;
  error?: string;
  enabled: boolean;
  url: string;
  default_backend: boolean;
  recipe: string;
  local_default?: { endpoint_id?: string; model?: string };
  nodes: SparkNode[];
  cluster?: { online?: number; total?: number; memory_total?: number; memory_used?: number; gpu_util?: number | null; power_w?: number | null };
  deployments: SparkDeployment[];
  recipes: SparkRecipe[];
  jobs: { id: string; kind: string; title: string; state: string; progress?: number | null }[];
  effective?: { endpoint_id: string; model: string; on_sparks: boolean };
}

export const SPARKS_POLL_MS = 3000;
export const SPARKS_IDLE_MS = 15000;

export function parseSparks(raw: unknown): SparksState | null {
  if (!raw || typeof raw !== 'object') return null;
  const b = raw as Record<string, unknown>;
  return {
    ok: b.ok === true,
    error: typeof b.error === 'string' ? b.error : undefined,
    enabled: b.enabled !== false,
    url: typeof b.url === 'string' ? b.url : '',
    default_backend: b.default_backend !== false,
    recipe: typeof b.recipe === 'string' ? b.recipe : '',
    local_default: (b.local_default as SparksState['local_default']) ?? {},
    nodes: Array.isArray(b.nodes) ? (b.nodes as SparkNode[]) : [],
    cluster: (b.cluster as SparksState['cluster']) ?? {},
    deployments: Array.isArray(b.deployments) ? (b.deployments as SparkDeployment[]) : [],
    recipes: Array.isArray(b.recipes) ? (b.recipes as SparkRecipe[]) : [],
    jobs: Array.isArray(b.jobs) ? (b.jobs as SparksState['jobs']) : [],
    effective: b.effective as SparksState['effective'],
  };
}

export async function loadSparks(signal?: AbortSignal, recipes = true): Promise<SparksState | null> {
  try {
    return parseSparks(await getJson<unknown>(`/api/sparks/status?recipes=${recipes ? 'true' : 'false'}`, signal));
  } catch {
    return null;
  }
}

async function send(method: string, path: string, body: unknown, timeoutMs = 60000): Promise<Record<string, unknown>> {
  try {
    const response = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) return { ok: false, error: String(data.detail || data.error || `HTTP ${response.status}`) };
    return data;
  } catch (err) {
    return { ok: false, error: (err as Error).message || 'network error' };
  }
}

/** Load or unload a recipe. A conflict comes back as `{ok: false, status: 409, conflicts: [...]}`. */
export function deployRecipe(recipe: string, action: 'start' | 'stop', stopConflicts = false) {
  return send('POST', '/api/sparks/deploy', { recipe, action, stop_conflicts: stopConflicts }, action === 'stop' ? 900000 : 90000);
}

export function saveSparksSettings(patch: { enabled?: boolean; url?: string; default_backend?: boolean; recipe?: string }) {
  return send('PUT', '/api/sparks/settings', patch, 30000);
}

export function syncSparks() {
  return send('POST', '/api/sparks/sync', {}, 30000);
}

/** The cluster state, refreshed every 3 s while `active` (a popover open), every 15 s otherwise, paused when the tab is hidden. */
export function useSparks(active: boolean): { state: SparksState | null; history: Record<string, number[]>; refresh: () => Promise<void> } {
  const [state, setState] = useState<SparksState | null>(null);
  const [history, setHistory] = useState<Record<string, number[]>>({});
  const activeRef = useRef(active);
  activeRef.current = active;
  const refresh = useCallback(async () => {
    const next = await loadSparks(undefined, activeRef.current);
    setState((prev) => (next && !next.recipes.length && prev?.recipes.length ? { ...next, recipes: prev.recipes } : next));
    if (next?.ok) {
      setHistory((prev) => {
        const out: Record<string, number[]> = {};
        // Only measured samples enter the trace: a missing reading is a gap, not a zero.
        for (const n of next.nodes) {
          const v = n.online ? n.gpu?.util : null;
          out[n.id] = typeof v === 'number' ? [...(prev[n.id] ?? []), v].slice(-40) : prev[n.id] ?? [];
        }
        return out;
      });
    }
  }, []);
  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    let first = true;
    const tick = async () => {
      // The first read happens even in a background tab, so the pill is there when the tab comes forward.
      if (first || !document.hidden) await refresh();
      first = false;
      if (!stopped) timer = window.setTimeout(tick, activeRef.current ? SPARKS_POLL_MS : SPARKS_IDLE_MS);
    };
    void tick();
    const onVisible = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh]);
  useEffect(() => {
    if (active) void refresh();
  }, [active, refresh]);
  return { state, history, refresh };
}

export function gbOf(bytes?: number | null, digits = 0): string {
  if (bytes == null) return '—';
  return (bytes / 1e9).toFixed(digits);
}

export function fmtCtxShort(n?: number | null): string {
  if (!n) return '';
  if (n >= 1_000_000) return `${(n / 1_048_576).toFixed(n % 1_048_576 ? 1 : 0)}M`;
  return `${Math.round(n / 1024)}K`;
}
