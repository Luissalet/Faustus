import { t } from '../i18n';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';

/**
 * Loop Cards (`/api/loop-cards`): the metric of a goal, how noisy it is and what counts as an
 * improvement. A check is drawn as a win or a loss only when the server said so against the
 * threshold; a difference inside it is always "within the noise".
 */
export type Verdict = 'win' | 'loss' | 'noise';
export interface Stats { n: number; mean: number; median: number; stdev: number; min: number; max: number; cv: number | null; threshold: number }
export interface LastCheck { at: string; verdict: Verdict; delta: number; delta_pct: number | null; mean: number; noise_multiple: number | null }
export interface LoopCardRow {
  id: string; project: string; goal: string; command: string; cwd: string | null; direction: 'up' | 'down'; repeats: number;
  extract: { kind: 'regex' | 'json' | 'time'; pattern?: string; path?: string }; created: string; updated: string;
  baseline: Stats; noisy: boolean; last_check: LastCheck | null; checks: number;
}
export interface CheckResult {
  id: string; verdict: Verdict; delta: number; delta_pct: number | null; better_by: number; threshold: number;
  noise_multiple: number | null; threshold_multiple: number | null; summary: Stats & { values: number[] }; notes: string[]; at: string;
}
export interface NewCard {
  goal: string; command: string; extract: { kind: 'regex' | 'json' | 'time'; pattern?: string; path?: string };
  direction: 'up' | 'down'; repeats: number; cwd?: string; project?: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION,
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok) {
    const o = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    throw new ApiError(String(o.error || o.detail || `${path} ${t('responded')} ${response.status}`), response.status);
  }
  return body as T;
}

const BASE = '/api/loop-cards';

export async function listCards(project?: string | null): Promise<LoopCardRow[]> {
  const r = await request<{ cards?: LoopCardRow[] }>(project ? `${BASE}?project=${encodeURIComponent(project)}` : BASE);
  return Array.isArray(r.cards) ? r.cards : [];
}
export const createCard = (card: NewCard) => request<LoopCardRow>(BASE, { method: 'POST', body: JSON.stringify(card) });
export const checkCard = (id: string, opts: { rebase?: boolean } = {}) =>
  request<CheckResult>(`${BASE}/${encodeURIComponent(id)}/check`, { method: 'POST', body: JSON.stringify(opts) });
export const deleteCard = (id: string) => request<{ deleted: string }>(`${BASE}/${encodeURIComponent(id)}`, { method: 'DELETE' });

export function verdictLabel(v: Verdict): string {
  return { win: t('Better'), loss: t('Worse'), noise: t('Within the noise') }[v];
}
export function verdictTone(v: Verdict): 'good' | 'bad' | 'unknown' {
  return v === 'win' ? 'good' : v === 'loss' ? 'bad' : 'unknown';
}
export function fmt(x: number | null | undefined, digits = 4): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '–';
  return Number(x.toPrecision(digits)).toString();
}
export function metricLabel(e: LoopCardRow['extract']): string {
  if (e.kind === 'time') return t('run time (s)');
  return e.kind === 'json' ? `JSON ${e.path ?? ''}` : `regex ${e.pattern ?? ''}`;
}
