import { ApiError, getJson } from './api';
import { getPref, setPref } from './memory';

/**
 * Skills: the procedures the assistant learned (or was taught) and can
 * inject into a conversation. Same endpoints as skills.js —
 * `/api/skills`, `/api/skills/{name}/markdown|test|test-status|
 * test-approval`, `/api/skills/audit-all`, `/api/skills/import-from-url`,
 * `/api/skills/add` — and the five preferences under `/api/prefs`.
 */

export type SkillStatus = 'draft' | 'published' | string;
export type AuditVerdict = 'pass' | 'needs_work' | 'fail' | 'inconclusive' | '';

export interface Necessity {
  necessary: boolean | null;
  reason: string;
  redundantWith: string[];
}

export interface Skill {
  name: string;
  description: string;
  category: string;
  tags: string[];
  status: SkillStatus;
  /** 0–100. */
  confidence: number;
  source: string;
  teacherModel: string;
  owner: string;
  created: number;
  updated: number;
  whenToUse: string;
  procedure: string[];
  pitfalls: string;
  verification: string;
  uses: number;
  lastUsed: number;
  path: string;
  auditVerdict: AuditVerdict;
  auditByTeacher: boolean;
  auditWorkerModel: string;
  auditTeacherModel: string;
  auditedAt: number;
  necessity: Necessity | null;
}

const str = (v: unknown) => (typeof v === 'string' ? v : '');
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : Number(v) || 0);
const list = (v: unknown) => (Array.isArray(v) ? v.map((x) => String(x ?? '')).filter(Boolean) : []);

function skillFrom(raw: Record<string, unknown>): Skill {
  const nec = raw.necessity && typeof raw.necessity === 'object' ? (raw.necessity as Record<string, unknown>) : null;
  const conf = num(raw.confidence);
  const procedure = Array.isArray(raw.procedure) ? list(raw.procedure) : Array.isArray(raw.steps) ? list(raw.steps) : str(raw.solution) ? [str(raw.solution)] : [];
  return {
    name: str(raw.name) || str(raw.id),
    description: str(raw.description) || str(raw.title),
    category: str(raw.category) || 'general',
    tags: list(raw.tags),
    status: str(raw.status) || 'draft',
    confidence: Math.round(conf <= 1 ? conf * 100 : conf),
    source: str(raw.source) || 'user',
    teacherModel: str(raw.teacher_model),
    owner: str(raw.owner),
    created: num(raw.created_at ?? raw.created),
    updated: num(raw.updated_at ?? raw.created_at ?? raw.created),
    whenToUse: str(raw.when_to_use) || str(raw.problem),
    procedure,
    pitfalls: str(raw.pitfalls),
    verification: str(raw.verification),
    uses: num(raw.uses),
    lastUsed: num(raw.last_used),
    path: str(raw.path),
    auditVerdict: str(raw.audit_verdict) as AuditVerdict,
    auditByTeacher: Boolean(raw.audit_by_teacher),
    auditWorkerModel: str(raw.audit_worker_model),
    auditTeacherModel: str(raw.audit_teacher_model),
    auditedAt: num(raw.audited_at),
    necessity: nec
      ? { necessary: typeof nec.necessary === 'boolean' ? nec.necessary : null, reason: str(nec.reason), redundantWith: list(nec.redundant_with) }
      : null,
  };
}

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown; error?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (typeof body.error === 'string') detail = body.error;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  credentials: 'same-origin',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const enc = (name: string) => encodeURIComponent(name);

export async function listSkills(signal?: AbortSignal): Promise<Skill[]> {
  const data = await getJson<{ skills?: Record<string, unknown>[] }>('/api/skills', signal);
  return (data.skills ?? []).map(skillFrom).filter((s) => s.name);
}

/** `null` when the entry predates SKILL.md files (the server answers 404). */
export async function skillMarkdown(name: string): Promise<string | null> {
  try {
    const data = await getJson<{ markdown?: string }>(`/api/skills/${enc(name)}/markdown`);
    return typeof data.markdown === 'string' ? data.markdown : '';
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

const yamlStr = (v: string) => JSON.stringify(v);

/** A SKILL.md in the server's own layout, from what the index knows about the skill. */
export function draftMarkdown(s: Skill): string {
  const fm = [
    `name: ${yamlStr(s.name)}`,
    `description: ${yamlStr(s.description)}`,
    'version: "1.0.0"',
    `category: ${yamlStr(s.category || 'general')}`,
    s.tags.length ? `tags: [${s.tags.map(yamlStr).join(', ')}]` : '',
    `status: ${s.status === 'published' ? 'published' : 'draft'}`,
    `confidence: ${(s.confidence / 100).toFixed(3)}`,
    `source: ${yamlStr(s.source || 'user')}`,
    s.owner ? `owner: ${yamlStr(s.owner)}` : '',
  ].filter(Boolean);
  const body = [
    s.whenToUse ? `## When to Use\n\n${s.whenToUse}` : '',
    s.procedure.length ? `## Procedure\n\n${s.procedure.map((x, i) => `${i + 1}. ${x}`).join('\n')}` : '',
    s.pitfalls ? `## Pitfalls\n\n- ${s.pitfalls}` : '',
    s.verification ? `## Verification\n\n- ${s.verification}` : '',
  ].filter(Boolean);
  return `---\n${fm.join('\n')}\n---\n\n${body.join('\n\n')}\n`;
}

export async function saveSkillMarkdown(name: string, markdown: string): Promise<void> {
  await ok(await fetch(`/api/skills/${enc(name)}/markdown`, json({ markdown })), 'skills/markdown');
}

export async function setSkillStatus(name: string, status: 'draft' | 'published'): Promise<void> {
  await ok(await fetch(`/api/skills/${enc(name)}`, { ...json({ status }), method: 'PUT' }), 'skills/status');
}

export async function deleteSkill(name: string): Promise<void> {
  await ok(await fetch(`/api/skills/${enc(name)}`, { method: 'DELETE', credentials: 'same-origin' }), 'skills/delete');
}

/* ── Test one skill: a run the server keeps; we poll its status ── */

export type TestEvent = {
  type: string;
  task?: string;
  model?: string;
  round?: number;
  tool?: string;
  command?: string;
  output?: string;
  text?: string;
  error?: string;
};

export interface TestApproval {
  approvalId: string;
  question: string;
  action: { tool: string; content: string; effects: string[]; workspace: string; digest: string } | null;
}

export interface TestVerdict {
  verdict: 'pass' | 'needs_work' | 'fail' | 'inconclusive' | 'unknown';
  confidence: number | null;
  summary: string;
  issues: string[];
}

export interface TestJob {
  status: 'none' | 'running' | 'awaiting_approval' | 'done';
  log: TestEvent[];
  approval: TestApproval | null;
  verdict: TestVerdict | null;
}

function jobFrom(raw: Record<string, unknown>): TestJob {
  const status = str(raw.status) as TestJob['status'];
  const ap = raw.approval && typeof raw.approval === 'object' ? (raw.approval as Record<string, unknown>) : null;
  const act = ap?.action && typeof ap.action === 'object' ? (ap.action as Record<string, unknown>) : null;
  const v = raw.verdict && typeof raw.verdict === 'object' ? (raw.verdict as Record<string, unknown>) : null;
  return {
    status: status === 'running' || status === 'awaiting_approval' || status === 'done' ? status : 'none',
    log: Array.isArray(raw.log) ? (raw.log as TestEvent[]) : [],
    approval: ap
      ? {
          approvalId: str(ap.approval_id),
          question: str(ap.question),
          action: act ? { tool: str(act.tool), content: str(act.content), effects: list(act.effects), workspace: str(act.workspace), digest: str(act.digest) } : null,
        }
      : null,
    verdict: v
      ? {
          verdict: (['pass', 'needs_work', 'fail', 'inconclusive'].includes(str(v.verdict)) ? str(v.verdict) : 'unknown') as TestVerdict['verdict'],
          confidence: typeof v.confidence === 'number' ? v.confidence : null,
          summary: str(v.summary),
          issues: list(v.issues),
        }
      : null,
  };
}

export async function testStatus(name: string): Promise<TestJob> {
  try {
    return jobFrom(await getJson<Record<string, unknown>>(`/api/skills/${enc(name)}/test-status`));
  } catch {
    return { status: 'none', log: [], approval: null, verdict: null };
  }
}

/** Starts a run with the model the previous interface would pick: the session's, else the default. */
export async function startTest(name: string, model = '', endpointUrl = ''): Promise<void> {
  await ok(await fetch(`/api/skills/${enc(name)}/test`, json({ model, endpoint_url: endpointUrl })), 'skills/test');
}

export async function decideTestApproval(name: string, approvalId: string, decision: 'approve' | 'deny'): Promise<void> {
  await ok(await fetch(`/api/skills/${enc(name)}/test-approval`, json({ approval_id: approvalId, decision })), 'skills/test-approval');
}

/* ── Audit all: test → fix → retry → teacher → flag, server-side ── */

export interface AuditState {
  status: 'none' | 'running' | 'cancelled' | 'done';
  done: number;
  total: number;
  current: string;
  results: { name: string; result: string }[];
  log: string[];
  teacher: string;
  summary: string;
}

function auditFrom(raw: Record<string, unknown>): AuditState {
  const status = str(raw.status) as AuditState['status'];
  return {
    status: status === 'running' || status === 'cancelled' || status === 'done' ? status : 'none',
    done: num(raw.done),
    total: num(raw.total),
    current: str(raw.current),
    results: Array.isArray(raw.results) ? (raw.results as Record<string, unknown>[]).map((r) => ({ name: str(r.name), result: str(r.result) })) : [],
    log: list(raw.log),
    teacher: str(raw.teacher),
    summary: str(raw.summary),
  };
}

export async function auditStatus(): Promise<AuditState> {
  try {
    return auditFrom(await getJson<Record<string, unknown>>('/api/skills/audit-all/status'));
  } catch {
    return { status: 'none', done: 0, total: 0, current: '', results: [], log: [], teacher: '', summary: '' };
  }
}

export async function startAudit(names: string[], scope: 'selected' | 'all', skipAudited: boolean): Promise<void> {
  await ok(await fetch('/api/skills/audit-all', json({ scope, names, skip_audited: skipAudited })), 'skills/audit-all');
}

export async function cancelAudit(): Promise<void> {
  await ok(await fetch('/api/skills/audit-all/cancel', { method: 'POST', credentials: 'same-origin' }), 'skills/audit-cancel');
}

/* ── Adding ── */

export async function importSkillFromUrl(url: string): Promise<{ name: string; files: number }> {
  const response = await ok(await fetch('/api/skills/import-from-url', json({ url })), 'skills/import');
  const data = (await response.json()) as { skill?: { name?: string }; files?: number };
  return { name: data.skill?.name ?? '', files: typeof data.files === 'number' ? data.files : 1 };
}

export interface NewSkill {
  name?: string;
  description: string;
  whenToUse: string;
  /** One step per line; leading bullets and numbers are stripped. */
  procedure: string;
  tags: string;
  category?: string;
}

export async function addSkill(input: NewSkill): Promise<void> {
  const procedure = input.procedure
    .split('\n')
    .map((s) => s.replace(/^\s*(?:[-*]|\d+[.)])\s+/, '').trim())
    .filter(Boolean);
  const tags = input.tags.split(',').map((s) => s.trim()).filter(Boolean);
  await ok(
    await fetch(
      '/api/skills/add',
      json({
        name: input.name?.trim() || undefined,
        description: input.description.trim(),
        category: input.category?.trim() || 'general',
        when_to_use: input.whenToUse.trim(),
        procedure,
        tags,
        status: 'draft',
      }),
    ),
    'skills/add',
  );
}

/* ── Preferences ── */

export interface SkillPrefs {
  enabled: boolean;
  autoExtract: boolean;
  autoApprove: boolean;
  /** 0–1. */
  minConfidence: number;
  maxInjected: number;
}

export async function loadSkillPrefs(): Promise<SkillPrefs> {
  const [enabled, autoExtract, autoApprove, minConfidence, maxInjected] = await Promise.all([
    getPref<boolean>('skills_enabled', false),
    getPref<boolean>('auto_skills', false),
    getPref<boolean>('auto_approve_skills', false),
    getPref<number>('skill_min_confidence', 0.85),
    getPref<number>('skill_max_injected', 3),
  ]);
  const min = Number(minConfidence);
  return {
    enabled: Boolean(enabled),
    autoExtract: Boolean(autoExtract),
    autoApprove: Boolean(autoApprove),
    minConfidence: Number.isFinite(min) && min > 0 ? Math.min(1, min) : 0.85,
    maxInjected: Number.isFinite(Number(maxInjected)) ? Math.max(0, Math.min(12, Number(maxInjected))) : 3,
  };
}

export { setPref as setSkillPref };

/* ── Client-side judgement, same rules as skills.js ── */

const STOP = new Set(['the', 'and', 'with', 'for', 'from', 'using']);

function tokens(s: Skill): Set<string> {
  return new Set(
    [s.name, s.description, s.whenToUse, ...s.tags]
      .join(' ')
      .toLowerCase()
      .replace(/-\d+\b/g, '')
      .split(/[^a-z0-9]+/)
      .filter((w) => w.length > 2 && !STOP.has(w)),
  );
}

function similarity(a: Skill, b: Skill): number {
  const A = tokens(a);
  const B = tokens(b);
  if (!A.size || !B.size) return 0;
  let inter = 0;
  for (const w of A) if (B.has(w)) inter++;
  return inter / (A.size + B.size - inter);
}

const baseName = (name: string) => name.replace(/-\d+$/, '');

function keeperScore(s: Skill): number {
  return (s.status === 'published' ? 100000 : 0) + s.uses * 100 + s.confidence + (s.auditByTeacher ? -5 : 0) - s.name.length / 1000;
}

export interface DuplicateInfo {
  group: number;
  keep: boolean;
  keepName: string;
  names: string[];
}

/** Groups look-alikes (same base name, or token similarity ≥ 0.38) and picks the one worth keeping. */
export function duplicateGroups(skills: Skill[]): Map<string, DuplicateInfo> {
  const parent = new Map<string, string>();
  for (const s of skills) parent.set(s.name, s.name);
  const find = (x: string): string => {
    let p = parent.get(x) ?? x;
    while (p !== parent.get(p)) p = parent.get(p) ?? p;
    return p;
  };
  for (let i = 0; i < skills.length; i++) {
    for (let j = i + 1; j < skills.length; j++) {
      const a = skills[i];
      const b = skills[j];
      if (baseName(a.name) === baseName(b.name) || similarity(a, b) >= 0.38) {
        const pa = find(a.name);
        const pb = find(b.name);
        if (pa !== pb) parent.set(pb, pa);
      }
    }
  }
  const groups = new Map<string, Skill[]>();
  for (const s of skills) {
    const root = find(s.name);
    const g = groups.get(root) ?? [];
    g.push(s);
    groups.set(root, g);
  }
  const meta = new Map<string, DuplicateInfo>();
  let idx = 1;
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const sorted = group.slice().sort((a, b) => keeperScore(b) - keeperScore(a));
    const keepName = sorted[0].name;
    const names = sorted.map((s) => s.name);
    for (const s of sorted) meta.set(s.name, { group: idx, keep: s.name === keepName, keepName, names });
    idx++;
  }
  return meta;
}

export type NecessityKind = 'duplicate' | 'trivial' | 'irrelevant' | null;

export function necessityKind(s: Skill, dup?: DuplicateInfo): NecessityKind {
  if (dup) return 'duplicate';
  const nec = s.necessity;
  if (!nec || nec.necessary !== false) return null;
  const reason = nec.reason.toLowerCase();
  if (nec.redundantWith.length || /duplicat|redundan|overlap|same skill|same procedure/.test(reason)) return 'duplicate';
  if (/trivial|generic|capable assistant|without a saved|not need|unnecessary/.test(reason)) return 'trivial';
  return 'irrelevant';
}

/** What "delete non-passing" would remove: duplicates, generic/irrelevant, failed audits, below threshold. */
export function needsAttention(s: Skill, dup: DuplicateInfo | undefined, threshold: number): boolean {
  if (necessityKind(s, dup)) return true;
  if (s.auditVerdict !== 'pass') return true;
  return s.confidence < Math.round(threshold * 100);
}

export type SkillSort = 'confidence' | 'uses' | 'alpha' | 'recent';

export function sortSkills(skills: Skill[], sort: SkillSort): Skill[] {
  const arr = skills.slice();
  const byName = (a: Skill, b: Skill) => a.name.localeCompare(b.name);
  if (sort === 'confidence') arr.sort((a, b) => b.confidence - a.confidence || byName(a, b));
  else if (sort === 'uses') arr.sort((a, b) => b.uses - a.uses || byName(a, b));
  else if (sort === 'recent') arr.sort((a, b) => b.updated - a.updated || byName(a, b));
  else arr.sort(byName);
  return arr;
}

export function matchesSkill(s: Skill, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q) || s.whenToUse.toLowerCase().includes(q) || s.category.toLowerCase().includes(q) || s.tags.some((x) => x.toLowerCase().includes(q));
}

export const shortModel = (model: string) => model.split('/').filter(Boolean).pop() ?? model;
