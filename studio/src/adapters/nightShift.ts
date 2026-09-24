import { getJson } from './api';

/**
 * Night shift (lot E): the Studio half of `/api/night-shift/*`
 * (`docs/api/night_shift.md`, `src/night_shift.py`).
 */

export type NightShiftState = 'queued' | 'running' | 'done' | 'stopped' | 'budget_exhausted' | 'error';

export interface NightShiftBudget {
  max_minutes: number;
  max_tasks: number;
  max_tokens?: number | null;
}

export interface NightShiftResult {
  task: string;
  job_id?: string;
  status?: string;
  verdict?: string;
  files_changed?: string[];
  verification?: Record<string, unknown> | null;
  needs_attention?: boolean;
  error?: string;
}

export interface NightShift {
  id: string;
  owner: string;
  workspace: string;
  tasks: string[];
  budget: NightShiftBudget;
  model?: string | null;
  verify: boolean;
  started: number | null;
  finished: number | null;
  state: NightShiftState;
  results: NightShiftResult[];
  skipped?: string[];
}

async function ok(r: Response, what: string): Promise<Response> {
  if (r.ok) return r;
  let msg = `${what}: HTTP ${r.status}`;
  try {
    const d = (await r.json()) as { detail?: string };
    if (d.detail) msg = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail);
  } catch {
    /* not json */
  }
  throw new Error(msg);
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

export const listNightShifts = () => getJson<{ shifts: NightShift[] }>('/api/night-shift').then((b) => b.shifts ?? []);

export const getNightShift = (id: string) => getJson<NightShift>(`/api/night-shift/${encodeURIComponent(id)}`);

export async function startNightShift(spec: {
  tasks: string[];
  workspace: string;
  budget?: Partial<NightShiftBudget>;
  model?: string;
  verify?: boolean;
}): Promise<NightShift> {
  const r = await ok(
    await fetch('/api/night-shift', {
      method: 'POST',
      credentials: 'same-origin',
      headers: JSON_HEADERS,
      body: JSON.stringify(spec),
    }),
    'night-shift',
  );
  return (await r.json()) as NightShift;
}

export async function stopNightShift(id: string): Promise<boolean> {
  const r = await ok(
    await fetch(`/api/night-shift/${encodeURIComponent(id)}/stop`, { method: 'POST', credentials: 'same-origin' }),
    'night-shift/stop',
  );
  const body = (await r.json()) as { stopped?: boolean };
  return Boolean(body.stopped);
}

export async function getNightShiftReport(id: string): Promise<string> {
  const r = await ok(
    await fetch(`/api/night-shift/${encodeURIComponent(id)}/report`, { credentials: 'same-origin' }),
    'night-shift/report',
  );
  return r.text();
}
