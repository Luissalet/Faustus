import { getJson } from './api';

/**
 * Swarm map: the Studio half of `/api/swarm/*` (`routes/swarm_routes.py`,
 * `src/swarm/`). Runs are started by the `swarm_map` tool in a chat; this
 * screen lists them, shows progress and offers the result files.
 */

export type SwarmStatus = 'queued' | 'running' | 'done' | 'partial' | 'failed' | 'cancelled' | 'interrupted';

export interface SwarmCounts {
  total: number;
  ok: number;
  failed: number;
  pending: number;
}

export interface SwarmParallel {
  parallel?: number;
  effective?: number;
  source?: string;
  backend?: string;
  local?: boolean;
}

export interface SwarmRun {
  run_id: string;
  status: SwarmStatus;
  mode: 'llm' | 'agent';
  instruction: string;
  session_id?: string;
  created_at: number;
  started_at?: number | null;
  finished_at?: number | null;
  counts: SwarmCounts;
  progress: number;
  parallel?: SwarmParallel | null;
  resolved?: { endpoint_url?: string; model?: string; source?: string } | null;
  output_fields: string[];
  reduce?: string | null;
  reduce_status?: string | null;
  files: string[];
  error?: string | null;
  resumable: boolean;
}

export const isLiveSwarm = (s: SwarmStatus): boolean => s === 'queued' || s === 'running';

export const listSwarmRuns = () => getJson<{ runs: SwarmRun[] }>('/api/swarm').then((b) => b.runs ?? []);

export const swarmFileUrl = (runId: string, name: string): string =>
  `/api/swarm/${encodeURIComponent(runId)}/files/${encodeURIComponent(name)}`;

async function post(path: string): Promise<Record<string, unknown>> {
  const r = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (!r.ok) {
    let msg = `${path}: HTTP ${r.status}`;
    try {
      const d = (await r.json()) as { detail?: unknown };
      if (typeof d.detail === 'string') msg = d.detail;
    } catch {
      /* not json */
    }
    throw new Error(msg);
  }
  return (await r.json()) as Record<string, unknown>;
}

export const cancelSwarmRun = (runId: string) => post(`/api/swarm/${encodeURIComponent(runId)}/cancel`);

export const resumeSwarmRun = (runId: string) => post(`/api/swarm/${encodeURIComponent(runId)}/resume`);
