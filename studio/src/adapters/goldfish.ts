import { t } from '../i18n';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';

/**
 * Goldfish reviews (`/api/goldfish`, src/goldfish.py): fresh workers that never saw the
 * conversation review or help write a document; the elephant (the side with the context) judges
 * every finding. Runs are background jobs; the screen polls while one is running.
 */
export type Flow = 'review' | 'loop' | 'draft' | 'brainstorm' | 'panel' | 'drift';
export type Severity = 'blocker' | 'major' | 'nit';
export interface Finding {
  id: string; cid?: string; reviewer: string; reviewers?: string[]; title: string; severity: Severity; lines: string;
  description: string; rationale?: string; state?: string; sources?: string[];
}
export interface Decision { category: string; justification: string; edits: { find: string; replace: string; status?: string }[]; user?: string }
export interface Round { round: number; statuses: Record<string, string>; findings: Finding[]; clean: boolean; decisions?: Record<string, Decision>; applied?: string[]; file?: string }
export interface StageRow { stage: number; name: string; statuses?: Record<string, string>; findings?: Finding[]; file?: string; skipped?: string; paraphrase?: { proposal: string; system: string } | null }
export interface Run {
  id: string; flow: Flow; title: string; target: string | null; target_label: string; status: string; phase: string; created: string;
  finished?: string; counts: Record<Severity, number> | null; findings: Finding[]; error?: string | null; outcome?: string | null;
  log: { at: string; msg: string }[]; rounds?: Round[]; stages_done?: StageRow[]; consolidated_file?: string; final_file?: string;
  reconciliation_file?: string; decisions?: Record<string, Decision>; round?: number; models?: string[]; written_back?: boolean;
  artifact?: { name: string; text: string };
}
export interface RunRow { id: string; flow: Flow; title: string; status: string; phase: string; created: string; counts: Record<Severity, number> | null; error?: string | null }
export interface GoldfishMeta {
  reviewers: Record<string, string>; standards: string[]; stages: Record<string, string>; presets: Record<string, number[]>; flows: Flow[];
  models: { endpoint_id: string; model: string; name: string }[];
}
export interface NewRun {
  flow: Flow; path?: string; text?: string; brief?: string; context?: string; context_files?: string[]; reviewers?: string[];
  preset?: string; max_rounds?: number; auto_apply?: boolean; write_back?: boolean; models?: string[]; panel_mode?: string;
  model?: string; save_to?: string; title?: string; double?: boolean;
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
  try { body = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    const o = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    throw new ApiError(String(o.error || o.detail || `${path} ${t('responded')} ${response.status}`), response.status);
  }
  return body as T;
}

const BASE = '/api/goldfish';
export const loadMeta = () => request<GoldfishMeta>(`${BASE}/meta`);
export async function listRuns(): Promise<RunRow[]> {
  const r = await request<{ runs?: RunRow[] }>(BASE);
  return Array.isArray(r.runs) ? r.runs : [];
}
export const startRun = (body: NewRun) => request<Run>(BASE, { method: 'POST', body: JSON.stringify(body) });
export const getRun = (id: string, artifact?: string) =>
  request<Run>(`${BASE}/${encodeURIComponent(id)}${artifact ? `?artifact=${encodeURIComponent(artifact)}` : ''}`);
export const resolveRun = (id: string, body: { mode: 'auto_accept' | 'continue' | 'decisions'; decisions?: Record<string, string> }) =>
  request<unknown>(`${BASE}/${encodeURIComponent(id)}/resolve`, { method: 'POST', body: JSON.stringify(body) });
export const cancelRun = (id: string) => request<unknown>(`${BASE}/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
export const deleteRun = (id: string) => request<unknown>(`${BASE}/${encodeURIComponent(id)}`, { method: 'DELETE' });

export function flowLabel(f: Flow): string {
  return { review: t('Staged review'), loop: t('Review loop'), draft: t('Draft and review'), brainstorm: t('Brainstorm'),
    panel: t('Panel of models'), drift: t('Document vs code') }[f];
}
export function severityLabel(s: Severity): string {
  return { blocker: t('Blocker'), major: t('Major'), nit: t('Nit') }[s];
}
export function phaseLabel(phase: string): string {
  const m = /^(stage|round) (\d+)$/.exec(phase || '');
  if (m) return m[1] === 'stage' ? t('stage {n}', { n: m[2] }) : t('round {n}', { n: m[2] });
  return t(phase || '');
}
export function isLive(status: string): boolean {
  return status === 'running' || status === 'queued' || status === 'awaiting_decision';
}
