import { ApiError, responseReason } from './api';

/**
 * W2-G / CMP-13 — Alternatives (`/api/projects/{project_id}/alternatives/*`,
 * `src/alternatives.py`).
 *
 * Isolated experiments: one `goal`, one `base_ref`, several `alternatives`
 * that can each hold different uncommitted content on the same files at the
 * same time (a git worktree per alternative, a frozen directory snapshot, or
 * a text snapshot for a single document) without colliding with the user's
 * own main copy. `apply`/`combine` three-way merge a chosen alternative's
 * changes into the main copy and NEVER destroy a manual edit made there
 * since the experiment started — a conflict comes back as `AlternativesApiError`
 * with `errorClass === 'alternatives.apply_conflict'` and `payload.conflicts`,
 * same shape `GitApiError`'s `git.merge_conflict` already uses in `git.ts`,
 * and nothing is written on the server when that happens.
 */

export type IsolationKind = 'worktree' | 'snapshot_dir' | 'doc_version';
export type AltStatus = 'pending' | 'ready' | 'failed' | 'applied';

export interface AltCost {
  known_usd: number | 'unknown';
  unestimable: string[];
}

export interface TestsResult {
  command: string;
  exit_code: number | null;
  ok: boolean;
  output: string;
  ran_at: number;
  duration_s: number;
  timed_out: boolean;
}

export interface DiffSummary {
  files_changed: number;
  additions: number;
  deletions: number;
}

export interface Alternative {
  id: string;
  label: string;
  isolation: IsolationKind;
  path: string;
  run_id: string | null;
  status: AltStatus;
  cost: AltCost;
  diff_summary: DiffSummary | null;
  tests_result: TestsResult | null;
  content: string | null;
  created_at: number;
}

export type BaseKind = 'git_sha' | 'snapshot' | 'doc';

export interface Experiment {
  id: string;
  version: number;
  project_id: string;
  owner: string;
  goal: string;
  base_kind: BaseKind;
  base_ref: string;
  workspace: string | null;
  doc_base_content?: string;
  alternatives: Alternative[];
  created_at: number;
  applied: { alternative_id?: string; combine?: boolean; applied_at: number; files: string[] } | null;
}

export interface FileDiffStat {
  change: 'none' | 'added' | 'modified' | 'deleted';
  additions: number | null;
  deletions: number | null;
}

export interface CompareAlternative {
  id: string;
  label: string;
  status: AltStatus;
  diff_summary: DiffSummary | null;
  tests_result: TestsResult | null;
  files: Record<string, FileDiffStat>;
}

export interface CompareResult {
  experiment: Experiment;
  base_ref: string;
  alternatives: CompareAlternative[];
  contested_files: Record<string, string[]>;
}

export class AlternativesApiError extends ApiError {
  readonly errorClass: string | null;
  readonly conflicts: string[];
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'AlternativesApiError';
    this.errorClass = errorClass;
    this.payload = payload;
    this.conflicts = Array.isArray(payload.conflicts) ? (payload.conflicts as string[]) : [];
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
    throw new AlternativesApiError(await responseReason(response, path), response.status, errorClass, payload);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/alternatives`;

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
}

function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  });
}

function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' });
}

export function listExperiments(projectId: string, signal?: AbortSignal): Promise<{ experiments: Experiment[] }> {
  return get(base(projectId), signal);
}

export function getExperiment(projectId: string, expId: string, signal?: AbortSignal): Promise<Experiment> {
  return get(`${base(projectId)}/${encodeURIComponent(expId)}`, signal);
}

export function createExperiment(projectId: string, goal: string, workspace?: string): Promise<Experiment> {
  return post(base(projectId), { goal, workspace: workspace || undefined });
}

export function createDocExperiment(projectId: string, goal: string, baseContent: string): Promise<Experiment> {
  return post(`${base(projectId)}/doc`, { goal, base_content: baseContent });
}

export function deleteExperiment(projectId: string, expId: string): Promise<{ ok: boolean }> {
  return del(`${base(projectId)}/${encodeURIComponent(expId)}`);
}

export function compareExperiment(projectId: string, expId: string, signal?: AbortSignal): Promise<CompareResult> {
  return get(`${base(projectId)}/${encodeURIComponent(expId)}/compare`, signal);
}

export function addAlternative(
  projectId: string, expId: string, label: string, isolation?: IsolationKind,
): Promise<Alternative> {
  return post(`${base(projectId)}/${encodeURIComponent(expId)}/alternatives`, { label, isolation });
}

export function setDocContent(
  projectId: string, expId: string, altId: string, content: string,
): Promise<Alternative> {
  return put(`${base(projectId)}/${encodeURIComponent(expId)}/alternatives/${encodeURIComponent(altId)}/content`, { content });
}

export function runTests(
  projectId: string, expId: string, altId: string, command: string, timeout?: number,
): Promise<TestsResult> {
  return post(`${base(projectId)}/${encodeURIComponent(expId)}/alternatives/${encodeURIComponent(altId)}/tests`, { command, timeout });
}

export interface ApplyResult {
  applied_files: string[];
  skipped_same: boolean;
  content?: string;
}

export function applyAlternative(
  projectId: string, expId: string, altId: string, mineDocContent?: string,
): Promise<ApplyResult> {
  return post(`${base(projectId)}/${encodeURIComponent(expId)}/apply/${encodeURIComponent(altId)}`,
    { mine_doc_content: mineDocContent });
}

export interface CombineResult {
  applied: { path: string; from: string }[];
}

export function combine(
  projectId: string, expId: string, choices: Record<string, string>, mineDocContent?: string,
): Promise<CombineResult> {
  return post(`${base(projectId)}/${encodeURIComponent(expId)}/combine`, { choices, mine_doc_content: mineDocContent });
}

/** `true` when this alternative has never had a diff computed for it yet
 *  (`compare` was never called) — the list screen shows "not compared yet"
 *  rather than a false "no changes". */
export function neverCompared(alt: Alternative): boolean {
  return alt.diff_summary === null;
}

export function costLabel(cost: AltCost): string {
  return cost.known_usd === 'unknown' ? 'unknown' : `$${cost.known_usd.toFixed(4)}`;
}
