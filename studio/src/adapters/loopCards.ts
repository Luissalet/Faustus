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
  keep_bar?: { pct: number; abs: number } | null; min_pairs?: number | null; guard_command?: string | null;
  stop_rule?: string | null; timebox_min?: number | null; outcome?: string | null; ledger?: number; kept?: number;
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

export interface LedgerRow {
  at: string; source: string; arm: string; change: string; control_median?: number | null; arm_median?: number | null;
  delta?: number | null; delta_pct?: number | null; pairs?: number | null; wins?: number | null; ci?: (number | null)[];
  noise_floor?: number | null; verdict: string; why?: string;
}
export interface FullCard extends Omit<LoopCardRow, 'ledger'> { ledger?: LedgerRow[] }
export interface Plan { keep_bar_pct?: number | null; keep_bar_abs?: number | null; min_pairs?: number | null; guard_command?: string; stop_rule?: string; timebox_min?: number | null; reason?: string }
export type ArmVerdict = 'KEEP' | 'MINOR' | 'DROP' | 'INCONCLUSIVE';
export interface ArmStat {
  arm: string; pairs: number; failed?: number; control_failed?: number; median?: number | null; wins?: number;
  median_delta?: number | null; delta_pct?: number | null; ci_low?: number | null; ci_high?: number | null; p_value?: number | null;
  verdict: ArmVerdict; why: string;
}
export interface ArmsStats { control_median: number; bar: number; aa?: { noise_floor: number; pairs: number }; arms: ArmStat[]; warnings: string[] }
export interface ArmSpec { name: string; command?: string; cwd?: string; label?: string }
export interface ArmsResult { id: string; rounds: number; stats: ArmsStats; keep: string[] }
export interface ImproveArm { name: string; hypothesis: string; error?: string; files?: string[] }
export interface ImproveIteration {
  iteration: number; hypotheses?: string[]; arms: ImproveArm[]; stats?: ArmsStats; winner?: { arm: string; hypothesis: string; delta_pct?: number | null };
  applied?: boolean; verify?: { verdict: Verdict; delta_pct: number | null };
}
export interface ImproveJob {
  id: string; card_id: string; goal: string; status: string; phase: string; created: string; finished?: string; iteration: number;
  iterations: number; proposals: number; rounds: number; files: string[]; apply: boolean; winner?: { arm: string; hypothesis: string; delta_pct?: number | null } | null;
  applied: boolean; error?: string | null; log: { at: string; msg: string }[]; history: ImproveIteration[];
}
export interface NewImprove { id: string; files: string[]; proposals: number; iterations: number; rounds: number; guard_command?: string; apply: boolean; model?: string; hints?: string }
export interface MirrorSuggestion { key: string; title: string; signal: string; before: string; after: string; fix: string; metric: string }
export interface MirrorStats {
  typed: number; machine: number; machine_per_typed: number; chats: number; words: { median: number; p90: number };
  rates_pct_of_typed: Record<string, number>; kickoffs: { count: number; has_done_when_pct: number; has_stop_condition_pct: number };
  loop_smells: { name: string; still_broken: number; pasted_numbers: number }[];
  repeated_corrections: { class: string; phrase: string; chats: number }[];
  automation_candidates: { prompt: string; times: number }[];
  assistant: { replies: number; concessions: number; concessions_per_100: number };
  suggestions: MirrorSuggestion[]; trend: { suggestion: string; metric: string; before: number; after: number; verdict: string }[];
  window: { days: number }; report?: string;
}

export const getCard = (id: string) => request<FullCard>(`${BASE}/${encodeURIComponent(id)}`);
export const savePlan = (id: string, plan: Plan) => request<FullCard>(`${BASE}/${encodeURIComponent(id)}/plan`, { method: 'POST', body: JSON.stringify(plan) });
export const runArms = (id: string, body: { arms: ArmSpec[]; rounds: number; aa: boolean }) =>
  request<ArmsResult>(`${BASE}/${encodeURIComponent(id)}/arms`, { method: 'POST', body: JSON.stringify(body) });
export async function listImprove(card?: string): Promise<ImproveJob[]> {
  const r = await request<{ runs?: ImproveJob[] }>(card ? `/api/loop-improve?card=${encodeURIComponent(card)}` : '/api/loop-improve');
  return Array.isArray(r.runs) ? r.runs : [];
}
export const startImprove = (body: NewImprove) => request<ImproveJob>('/api/loop-improve', { method: 'POST', body: JSON.stringify(body) });
export const getImprove = (job: string) => request<ImproveJob>(`/api/loop-improve/${encodeURIComponent(job)}`);
export const cancelImprove = (job: string) => request<unknown>(`/api/loop-improve/${encodeURIComponent(job)}/cancel`, { method: 'POST' });
export const deleteImprove = (job: string) => request<unknown>(`/api/loop-improve/${encodeURIComponent(job)}`, { method: 'DELETE' });
export const runMirror = (days: number) => request<MirrorStats>('/api/prompt-mirror', { method: 'POST', body: JSON.stringify({ days }) });

export function armTone(v: string): 'good' | 'bad' | 'unknown' {
  return v === 'KEEP' || v === 'win' ? 'good' : v === 'DROP' || v === 'loss' ? 'bad' : 'unknown';
}
export function armLabel(v: string): string {
  const map: Record<string, string> = {
    KEEP: t('Keep'), MINOR: t('Minor'), DROP: t('Drop'), INCONCLUSIVE: t('Inconclusive'), win: t('Better'), loss: t('Worse'),
    noise: t('Within the noise'), baseline: t('Baseline'), plan: t('Plan changed'),
  };
  return map[v] ?? v;
}

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
