import { ApiError, getJson } from './api';

/**
 * Instincts (`src/instincts.py`, `routes/instincts_routes.py`) — small
 * "when X, do Y" patterns the assistant noticed from corrections in a
 * conversation, or that a person added by hand. Owner-scoped, not
 * admin-only: every call here only ever sees the signed-in owner's own
 * store.
 */

export type InstinctDomain = 'code-style' | 'workflow' | 'testing' | 'tooling' | 'communication' | 'debugging' | 'other';
export type InstinctScope = 'project' | 'global';

export interface InstinctEvidence {
  note?: string;
  [key: string]: unknown;
}

export interface Instinct {
  id: string;
  trigger: string;
  action: string;
  confidence: number;
  domain: InstinctDomain;
  scope: InstinctScope;
  project: string;
  project_name: string;
  source: string;
  evidence: InstinctEvidence[];
  observations: number;
  confirmations: number;
  contradictions: number;
  created: number;
  updated: number;
  last_observed: number;
  status: 'active' | 'retired';
  promoted_from: string[];
  effective_confidence: number;
}

export interface InstinctsStatus {
  total: number;
  by_scope: Record<string, number>;
  by_domain: Record<string, number>;
  top: Instinct[];
  pending_promotions: PromotionCandidate[];
}

export interface PromotionCandidate {
  base: string;
  projects: string[];
  mean_confidence: number;
  keys: string[];
}

export interface EvolveCluster {
  keywords: string[];
  instinct_ids: string[];
  suggested: 'agent' | 'command' | 'skill';
  title: string;
  mean_confidence: number;
  domain: string;
}

async function readError(response: Response, path: string): Promise<string> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown };
    if (typeof body?.detail === 'string' && body.detail.trim()) return body.detail;
  } catch {
    /* not JSON */
  }
  return `${path} responded ${response.status}`;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await readError(response, path), response.status);
  return (await response.json()) as T;
}

const API = '/api/instincts';
const enc = (s: string) => encodeURIComponent(s);

export async function listInstincts(opts?: { project?: string; minConfidence?: number }): Promise<Instinct[]> {
  const q = new URLSearchParams();
  if (opts?.project) q.set('project', opts.project);
  if (opts?.minConfidence) q.set('min_confidence', String(opts.minConfidence));
  const qs = q.toString();
  const data = await getJson<{ instincts: Instinct[]; count: number }>(`${API}${qs ? `?${qs}` : ''}`);
  return data.instincts ?? [];
}

export function instinctsStatus(project?: string): Promise<InstinctsStatus> {
  return getJson<InstinctsStatus>(`${API}/status${project ? `?project=${enc(project)}` : ''}`);
}

export function getInstinct(id: string): Promise<Instinct> {
  return getJson<Instinct>(`${API}/${enc(id)}`);
}

export interface NewInstinct {
  trigger: string;
  action: string;
  domain?: InstinctDomain;
  scope?: InstinctScope;
  project?: string;
  project_name?: string;
}

export function addInstinct(input: NewInstinct): Promise<Instinct> {
  return postJson<Instinct>(API, input);
}

export async function deleteInstinct(id: string): Promise<void> {
  const response = await fetch(`${API}/${enc(id)}`, { method: 'DELETE', credentials: 'same-origin' });
  if (!response.ok) throw new ApiError(await readError(response, `${API}/${id}`), response.status);
}

export function confirmInstinct(id: string, evidence?: string): Promise<Instinct> {
  return postJson<Instinct>(`${API}/${enc(id)}/confirm`, { evidence: evidence ? { note: evidence } : null });
}

export function contradictInstinct(id: string, evidence?: string): Promise<Instinct> {
  return postJson<Instinct>(`${API}/${enc(id)}/contradict`, { evidence: evidence ? { note: evidence } : null });
}

export function retireInstinct(id: string): Promise<Instinct> {
  return postJson<Instinct>(`${API}/${enc(id)}/retire`, {});
}

export interface PromoteResult {
  dry_run: boolean;
  candidates?: PromotionCandidate[];
  promoted?: string[];
}

export function promoteInstincts(opts?: { id?: string; dryRun?: boolean }): Promise<PromoteResult> {
  return postJson<PromoteResult>(`${API}/promote`, { id: opts?.id ?? null, dry_run: opts?.dryRun ?? false });
}

export interface EvolveResult {
  clusters: EvolveCluster[];
  created_skill_ids: string[];
}

export function evolveInstincts(opts?: { project?: string; generate?: boolean; minCluster?: number }): Promise<EvolveResult> {
  return postJson<EvolveResult>(`${API}/evolve`, {
    project: opts?.project ?? null,
    generate: opts?.generate ?? false,
    min_cluster: opts?.minCluster ?? 2,
  });
}

export async function exportInstincts(): Promise<string> {
  const data = await getJson<{ json: string }>(`${API}/export`);
  return data.json;
}

export interface ImportResult {
  imported: number;
  [key: string]: unknown;
}

export function importInstincts(payload: string, scopeOverride?: InstinctScope): Promise<ImportResult> {
  return postJson<ImportResult>(`${API}/import`, { json: payload, scope_override: scopeOverride ?? null });
}

export interface ExtractResult {
  stored: Instinct[];
  count: number;
}

export function extractInstincts(sessionId: string): Promise<ExtractResult> {
  return postJson<ExtractResult>(`${API}/extract`, { session_id: sessionId });
}

export const DOMAINS: InstinctDomain[] = ['code-style', 'workflow', 'testing', 'tooling', 'communication', 'debugging', 'other'];

export function downloadJson(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
