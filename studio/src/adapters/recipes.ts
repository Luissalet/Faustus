import { t } from '../i18n';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER, asArray } from './api';

/**
 * Learned recipes (`/api/recipes`), shaped for the "Recipes" screen.
 *
 * Two rules of PRODUCT live here as pure functions, not as lines in a
 * component:
 *
 * **A dry run is never a verification.** `/try` renders the steps on a second
 * case and runs nothing; whatever it answers, `canClaimVerified` is false for
 * it. Only a REAL execution whose status is `verified` (every step finished
 * and at least one exit criterion passed on the delivered files) may be drawn
 * as verified.
 *
 * **Unknown statuses read DOWN.** A status this file has not heard of is shown
 * as it came, in the neutral tone, never as success.
 */

export type RunStatus =
  | 'verified' | 'criteria_failed' | 'step_failed' | 'incomplete' | 'executed_unverified'
  | 'not_runnable' | 'busy';

export type Tone = 'good' | 'warn' | 'bad' | 'unknown';

export interface RecipeParam { name: string; kind: string; example: unknown; source: string; required: boolean }
export interface RecipeStepTemplate { index: number; tool: string; args: unknown }
export type Criterion = Record<string, unknown> & { kind: string };

export interface ExecutionRecord {
  at: number;
  status: string;
  verified: boolean;
  params: Record<string, unknown>;
  steps: { index: number; tool: string; status: string; detail: string }[];
  criteria: { kind: string; path: string | null; passed: boolean }[];
}

export interface Recipe {
  id: string;
  title: string;
  status: string;
  version: string;
  learned: boolean;
  params: RecipeParam[];
  stepTemplates: RecipeStepTemplate[];
  exitCriteria: Criterion[];
  executions: ExecutionRecord[];
  limits: string[];
}

export interface VerificationCase {
  caseKey: string;
  params: Record<string, unknown>;
  at: number;
  status: string;
  verified: boolean;
  fresh: boolean;
  stale: boolean;
  staleReasons: string[];
}

export interface StepResult {
  index: number;
  tool: string;
  status: string;
  detail: string;
  whyRun: string[];
  ids: Record<string, string>;
  output: string;
  waitedFor: string;
}

export interface CriterionResult {
  kind: string;
  path: string;
  passed: boolean;
  expected: string;
  observed: string;
  error: string;
}

export interface RunReport {
  mode: 'dry_run' | 'execute';
  /** Dry-run only: true while nothing was executed and nothing was checked. */
  structural: boolean;
  executed: boolean;
  verified: boolean;
  status: string;
  why: string[];
  steps: StepResult[];
  criteria: CriterionResult[];
  problems: string[];
  ranNow: number;
  reused: number;
  skipped: number;
}

/* -- small readers ------------------------------------------------------- */

const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const strList = (v: unknown): string[] => asArray<unknown>(v).map(str).filter(Boolean);

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function show(v: unknown): string {
  if (typeof v === 'string') return v.length > 300 ? `${v.slice(0, 300)}…` : v;
  try {
    const s = JSON.stringify(v);
    return s === undefined ? '' : s.length > 300 ? `${s.slice(0, 300)}…` : s;
  } catch {
    return str(v);
  }
}

/* -- statuses ------------------------------------------------------------- */

const STATUS: Record<string, { label: string; tone: Tone }> = {
  verified: { label: 'Verified', tone: 'good' },
  criteria_failed: { label: 'Criteria failed', tone: 'bad' },
  step_failed: { label: 'Step failed', tone: 'bad' },
  incomplete: { label: 'Incomplete', tone: 'warn' },
  executed_unverified: { label: 'Executed, not verified', tone: 'warn' },
  not_runnable: { label: 'Not runnable', tone: 'warn' },
  busy: { label: 'Busy', tone: 'warn' },
};

const STEP_STATUS: Record<string, { label: string; tone: Tone }> = {
  ok: { label: 'Step finished', tone: 'good' },
  reused: { label: 'Reused', tone: 'good' },
  resolved: { label: 'Renders with these parameters', tone: 'unknown' },
  unresolved: { label: 'Does not render', tone: 'bad' },
  error: { label: 'Step error', tone: 'bad' },
  exception: { label: 'Step error', tone: 'bad' },
  queued: { label: 'Not finished', tone: 'warn' },
  skipped: { label: 'Not run', tone: 'unknown' },
};

export function statusInfo(status: string): { label: string; tone: Tone } {
  const hit = STATUS[status];
  return hit ? { label: t(hit.label), tone: hit.tone } : { label: status || t('Never executed'), tone: 'unknown' };
}

export function stepStatusInfo(status: string): { label: string; tone: Tone } {
  const hit = STEP_STATUS[status];
  return hit ? { label: t(hit.label), tone: hit.tone } : { label: status, tone: 'unknown' };
}

/** Only a real execution that ended `verified` may be drawn as verified. */
export function canClaimVerified(report: Pick<RunReport, 'mode' | 'structural' | 'status' | 'verified'>): boolean {
  return report.mode === 'execute' && !report.structural && report.status === 'verified' && report.verified === true;
}

/* -- reading the wire ----------------------------------------------------- */

function readCriterion(raw: unknown): Criterion {
  const o = obj(raw);
  return { ...o, kind: str(o.kind) };
}

export function readRecipe(raw: unknown): Recipe {
  const r = obj(raw);
  const templates = asArray<Record<string, unknown>>(r.step_templates);
  return {
    id: str(r.id),
    title: str(r.title) || str(r.id),
    status: str(r.status),
    version: str(r.version),
    learned: templates.length > 0,
    params: asArray<Record<string, unknown>>(r.params).map((p) => ({
      name: str(p.name), kind: str(p.kind) || 'text', example: p.example, source: str(p.source), required: p.required !== false,
    })),
    stepTemplates: templates.map((s) => ({ index: num(s.index), tool: str(s.tool), args: s.args })),
    exitCriteria: asArray<unknown>(r.exit_criteria).map(readCriterion),
    executions: asArray<Record<string, unknown>>(r.executions).map((e) => ({
      at: num(e.at), status: str(e.status), verified: e.verified === true, params: obj(e.params),
      steps: asArray<Record<string, unknown>>(e.steps).map((s) => ({
        index: num(s.index), tool: str(s.tool), status: str(s.status), detail: str(s.detail),
      })),
      criteria: asArray<Record<string, unknown>>(e.criteria).map((c) => ({
        kind: str(c.kind), path: c.path == null ? null : str(c.path), passed: c.passed === true,
      })),
    })),
    limits: strList(r.limits),
  };
}

export function readVerification(raw: unknown): VerificationCase[] {
  return asArray<Record<string, unknown>>(obj(raw).cases).map((c) => ({
    caseKey: str(c.case_key), params: obj(c.params), at: num(c.at), status: str(c.status),
    verified: c.verified === true, fresh: c.fresh === true, stale: c.stale === true, staleReasons: strList(c.stale_reasons),
  }));
}

export function readRun(raw: unknown, mode: 'dry_run' | 'execute'): RunReport {
  const r = obj(raw);
  const structural = mode === 'dry_run' || r.kind === 'structural_dry_run';
  const steps = asArray<Record<string, unknown>>(r.steps).map((s): StepResult => ({
    index: num(s.index),
    tool: str(s.tool),
    // A dry run's step is "resolved"/"unresolved": it renders, nothing more.
    status: str(s.status),
    detail: str(s.detail) || str(s.reason),
    whyRun: strList(s.why_run),
    ids: Object.fromEntries(Object.entries(obj(s.ids)).map(([k, v]) => [k, str(v)])),
    output: s.output === undefined ? '' : show(s.output),
    waitedFor: str(s.waited_for),
  }));
  const criteria = asArray<Record<string, unknown>>(r.criteria).map((c): CriterionResult => ({
    kind: str(c.kind), path: str(c.path), passed: c.passed === true,
    expected: c.expected === undefined ? '' : show(c.expected),
    observed: c.observed === undefined ? '' : show(c.observed),
    error: str(c.error),
  }));
  const status = structural ? (r.promotable === true ? 'structural_ok' : 'structural') : str(r.status);
  return {
    mode, structural,
    executed: r.executed === true,
    // Structural runs are `verified: false` on the wire; the screen does not even read it for them.
    verified: !structural && r.verified === true,
    status,
    why: [...strList(r.why), ...strList(r.why_unverified), ...strList(r.why_not_promotable)],
    steps, criteria,
    problems: strList(r.problems),
    ranNow: num(r.ran_now), reused: num(r.reused), skipped: num(r.skipped),
  };
}

/* -- the forms ------------------------------------------------------------ */

/** One field per parameter, prefilled with the first case's value so a second case is an edit. */
export function initialParams(recipe: Recipe): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of recipe.params) out[p.name] = p.example == null ? '' : typeof p.example === 'string' ? p.example : show(p.example);
  return out;
}

/** Form text -> typed values: numbers and booleans by kind, everything else as written. */
export function typedParams(recipe: Recipe, values: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of recipe.params) {
    const raw = values[p.name] ?? '';
    if (p.kind === 'number' || p.kind === 'integer') {
      const n = Number(raw);
      out[p.name] = raw.trim() !== '' && Number.isFinite(n) ? n : raw;
    } else out[p.name] = raw;
  }
  return out;
}

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

export function parseParamsJson(text: string): Parsed<Record<string, unknown>> {
  try {
    const v: unknown = JSON.parse(text.trim() || '{}');
    if (!v || typeof v !== 'object' || Array.isArray(v)) return { ok: false, error: t('Parameters must be a JSON object {"name": value}.') };
    return { ok: true, value: v as Record<string, unknown> };
  } catch (e) {
    return { ok: false, error: `${t('Parameters are not valid JSON')}: ${(e as Error).message}` };
  }
}

export const CRITERIA_KINDS = ['file_exists', 'contains', 'regex', 'row_count', 'columns', 'number'];

export function parseCriteriaJson(text: string): Parsed<Criterion[]> {
  const raw = text.trim();
  if (!raw) return { ok: true, value: [] };
  let v: unknown;
  try {
    v = JSON.parse(raw);
  } catch (e) {
    return { ok: false, error: `${t('Criteria are not valid JSON')}: ${(e as Error).message}` };
  }
  if (!Array.isArray(v) || !v.every((c) => c && typeof c === 'object' && !Array.isArray(c))) {
    return { ok: false, error: t('Criteria must be a JSON list of objects {"kind": …, "path": …}.') };
  }
  for (const c of v as Record<string, unknown>[]) {
    if (!CRITERIA_KINDS.includes(str(c.kind))) {
      return { ok: false, error: `${t('Criterion kind must be one of')} ${CRITERIA_KINDS.join(', ')}` };
    }
  }
  return { ok: true, value: v as Criterion[] };
}

export function criteriaText(criteria: Criterion[]): string {
  return criteria.length ? JSON.stringify(criteria, null, 2) : '';
}

/** A starting point for the criteria editor, using the recipe's own parameters. */
export function criteriaExample(recipe: Recipe): string {
  const path = recipe.params.find((p) => p.kind === 'path' || p.kind === 'file') ?? recipe.params[0];
  const ref = path ? `{{${path.name}}}` : 'informe.md';
  return JSON.stringify([{ kind: 'file_exists', path: ref }], null, 2);
}

export function activeWorkspace(): string {
  try {
    return localStorage.getItem('odysseus-workspace') || '';
  } catch {
    return '';
  }
}

/* -- calls ---------------------------------------------------------------- */

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
    const o = obj(body);
    const reason = str(o.error) || str(o.detail) || `${path} ${t('responded')} ${response.status}`;
    throw new ApiError(reason, response.status);
  }
  return body as T;
}

export async function listRecipes(): Promise<Recipe[]> {
  const body = await request<unknown>('/api/recipes');
  return asArray<unknown>(obj(body).recipes).map(readRecipe).filter((r) => r.learned);
}

export async function loadRecipe(id: string): Promise<Recipe> {
  return readRecipe(obj(await request<unknown>(`/api/recipes/${encodeURIComponent(id)}`)).recipe);
}

export async function loadVerification(id: string): Promise<VerificationCase[]> {
  return readVerification(await request<unknown>(`/api/recipes/${encodeURIComponent(id)}/verification`));
}

export async function dryRun(id: string, params: Record<string, unknown>): Promise<RunReport> {
  const raw = await request<unknown>(`/api/recipes/${encodeURIComponent(id)}/try`, {
    method: 'POST', body: JSON.stringify({ params, mode: 'dry_run' }),
  });
  return readRun(raw, 'dry_run');
}

export async function executeRecipe(
  id: string,
  body: { params: Record<string, unknown>; workspace: string; criteria?: Criterion[]; jobTimeout?: number; rerun?: boolean },
): Promise<RunReport> {
  const payload: Record<string, unknown> = { params: body.params, workspace: body.workspace };
  if (body.criteria && body.criteria.length) payload.criteria = body.criteria;
  if (body.jobTimeout) payload.job_timeout = body.jobTimeout;
  if (body.rerun) payload.rerun = true;
  const raw = await request<unknown>(`/api/recipes/${encodeURIComponent(id)}/execute`, {
    method: 'POST', body: JSON.stringify(payload),
  });
  return readRun(raw, 'execute');
}
