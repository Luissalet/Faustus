import { ApiError, getJson } from './api';

/**
 * Memoria (Brain): `/api/memory` (the facts store), `/api/memory-engine`
 * (outcome-scored learned rules) and `/api/prefs` (the two switches). The
 * same endpoints memory.js uses, in the same shapes.
 */

export const MEMORY_CATEGORIES = ['fact', 'identity', 'preference', 'contact', 'project', 'goal', 'task'] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];

export const CATEGORY_LABEL: Record<string, string> = {
  fact: 'Fact',
  identity: 'Identity',
  preference: 'Preference',
  contact: 'Contact',
  project: 'Project',
  goal: 'Goal',
  task: 'Task',
};

export interface Memory {
  id: string;
  text: string;
  category: string;
  source: string;
  timestamp: number;
  uses: number;
  pinned: boolean;
  sessionId: string | null;
}

function memoryFrom(raw: Record<string, unknown>): Memory {
  return {
    id: String(raw.id),
    text: typeof raw.text === 'string' ? raw.text : '',
    category: typeof raw.category === 'string' && raw.category ? raw.category : 'fact',
    source: typeof raw.source === 'string' ? raw.source : 'user',
    timestamp: typeof raw.timestamp === 'number' ? raw.timestamp : 0,
    uses: typeof raw.uses === 'number' ? raw.uses : 0,
    pinned: Boolean(raw.pinned),
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : null,
  };
}

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

function form(fields: Record<string, string | undefined>): FormData {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v !== undefined) fd.append(k, v);
  return fd;
}

export async function listMemories(signal?: AbortSignal): Promise<Memory[]> {
  const data = await getJson<{ memory?: Record<string, unknown>[] }>('/api/memory', signal);
  return (data.memory ?? []).map(memoryFrom);
}

/** Returns false when the server said it already had that text. */
export async function addMemory(text: string, category: string, sessionId?: string | null): Promise<boolean> {
  // JSON, like memory.js: the route parses a JSON body first and only
  // falls back to a form when there is none (multipart gets a 422).
  const response = await ok(
    await fetch('/api/memory/add', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, category, source: 'user', session_id: sessionId ?? null }),
    }),
    'memory/add',
  );
  const data = (await response.json()) as { message?: string };
  return data.message !== 'Memory already exists';
}

export async function updateMemory(id: string, text: string, category?: string): Promise<void> {
  await ok(await fetch(`/api/memory/${encodeURIComponent(id)}`, { method: 'PUT', credentials: 'same-origin', body: form({ text, category }) }), 'memory/update');
}

export async function deleteMemory(id: string): Promise<void> {
  await ok(await fetch(`/api/memory/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' }), 'memory/delete');
}

export async function pinMemory(id: string, pinned: boolean): Promise<void> {
  await ok(await fetch(`/api/memory/${encodeURIComponent(id)}/pin`, { method: 'POST', credentials: 'same-origin', body: form({ pinned: pinned ? 'true' : 'false' }) }), 'memory/pin');
}

export interface AuditResult {
  before: number;
  after: number;
  removed: number;
  alreadyTidy: boolean;
}

/** LLM dedupe/consolidate ("Tidy" in the previous interface). */
export async function auditMemories(sessionId?: string | null): Promise<AuditResult> {
  const response = await ok(await fetch('/api/memory/audit', { method: 'POST', credentials: 'same-origin', body: form({ session: sessionId ?? undefined }) }), 'memory/audit');
  const data = (await response.json()) as { before?: number; after?: number; removed?: number; already_tidy?: boolean };
  return { before: data.before ?? 0, after: data.after ?? 0, removed: data.removed ?? 0, alreadyTidy: Boolean(data.already_tidy) };
}

/** Suggestions from a conversation's history. */
export async function extractFromSession(sessionId: string): Promise<string[]> {
  const response = await ok(await fetch('/api/memory/extract', { method: 'POST', credentials: 'same-origin', body: form({ session: sessionId }) }), 'memory/extract');
  const data = (await response.json()) as { suggestions?: unknown[] };
  return (data.suggestions ?? []).map((s) => (typeof s === 'string' ? s : String((s as { text?: unknown })?.text ?? ''))).filter(Boolean);
}

export interface ImportSuggestion {
  text: string;
  category: string;
}

/** Suggestions from a file (PDF, TXT, MD…). */
export async function importFromFile(file: File, sessionId?: string | null): Promise<{ suggestions: ImportSuggestion[]; message: string | null }> {
  const fd = new FormData();
  fd.append('file', file);
  if (sessionId) fd.append('session', sessionId);
  const response = await ok(await fetch('/api/memory/import', { method: 'POST', credentials: 'same-origin', body: fd }), 'memory/import');
  const data = (await response.json()) as { suggestions?: unknown[]; message?: string };
  const suggestions = (data.suggestions ?? [])
    .map((s) => (typeof s === 'string' ? { text: s, category: 'fact' } : { text: String((s as { text?: unknown })?.text ?? ''), category: String((s as { category?: unknown })?.category ?? 'fact') }))
    .filter((s) => s.text);
  return { suggestions, message: typeof data.message === 'string' ? data.message : null };
}

export function exportMemories(memories: Memory[]): Blob {
  const payload = memories.map((m) => ({ id: m.id, text: m.text, category: m.category, source: m.source, timestamp: m.timestamp, pinned: m.pinned }));
  return new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
}

/* ── Preferences ────────────────────────────────────────────────────────── */

export async function getPref<T>(key: string, fallback: T): Promise<T> {
  try {
    const data = await getJson<{ value?: unknown }>(`/api/prefs/${encodeURIComponent(key)}`);
    return data.value === undefined || data.value === null ? fallback : (data.value as T);
  } catch {
    return fallback;
  }
}

export async function setPref(key: string, value: unknown): Promise<void> {
  await ok(
    await fetch(`/api/prefs/${encodeURIComponent(key)}`, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }) }),
    'prefs',
  );
}

/* ── Learned rules (memory engine) ──────────────────────────────────────── */

export const RULE_LEVELS = ['working', 'episodic', 'semantic', 'procedural'] as const;

export interface LearnedRule {
  id: string;
  text: string;
  level: string;
  category: string;
  trustClass: string;
  status: 'active' | 'anti_pattern' | 'deprecated' | string;
  maturity: string;
  effectiveScore: number;
  harmfulRatio: number;
  project: string;
  helpful: number;
  harmful: number;
}

export interface RuleStats {
  total: number;
  active: number;
  antiPattern: number;
  deprecated: number;
  semanticLane: boolean;
}

function ruleFrom(raw: Record<string, unknown>): LearnedRule {
  const num = (v: unknown) => (Number.isFinite(Number(v)) ? Number(v) : 0);
  return {
    id: String(raw.id ?? ''),
    text: typeof raw.text === 'string' ? raw.text : '',
    level: typeof raw.level === 'string' && raw.level ? raw.level : 'semantic',
    category: typeof raw.category === 'string' ? raw.category : '',
    trustClass: typeof raw.trust_class === 'string' ? raw.trust_class : '',
    status: typeof raw.status === 'string' && raw.status ? raw.status : 'active',
    maturity: typeof raw.maturity === 'string' && raw.maturity ? raw.maturity : 'candidate',
    effectiveScore: num(raw.effective_score),
    harmfulRatio: num(raw.harmful_ratio),
    project: typeof raw.project === 'string' ? raw.project : '',
    helpful: num(raw.helpful_count),
    harmful: num(raw.harmful_count),
  };
}

export async function listRules(signal?: AbortSignal): Promise<{ rules: LearnedRule[]; stats: RuleStats }> {
  const data = await getJson<{ items?: Record<string, unknown>[]; stats?: Record<string, unknown> }>('/api/memory-engine/items', signal);
  const s = data.stats ?? {};
  return {
    rules: (data.items ?? []).filter((r) => r && r.id != null).map(ruleFrom),
    stats: {
      total: Number(s.total ?? 0),
      active: Number(s.active ?? 0),
      antiPattern: Number(s.anti_pattern ?? 0),
      deprecated: Number(s.deprecated ?? 0),
      semanticLane: Boolean(s.semantic_lane),
    },
  };
}

export async function addRule(text: string, level: string, category = ''): Promise<LearnedRule> {
  const response = await ok(
    await fetch('/api/memory-engine/items', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, level, category }) }),
    'memory-engine/items',
  );
  const data = (await response.json()) as { item?: Record<string, unknown> };
  return ruleFrom(data.item ?? {});
}

export async function ruleFeedback(id: string, kind: 'helpful' | 'harmful'): Promise<LearnedRule> {
  const response = await ok(
    await fetch(`/api/memory-engine/items/${encodeURIComponent(id)}/feedback`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }) }),
    'memory-engine/feedback',
  );
  const data = (await response.json()) as { item?: Record<string, unknown> };
  return ruleFrom(data.item ?? {});
}

export async function deleteRule(id: string): Promise<void> {
  await ok(await fetch(`/api/memory-engine/items/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' }), 'memory-engine/delete');
}

export interface CuratorReport {
  deduped: number;
  inverted: number;
  promoted: number;
  demoted: number;
  pruned: number;
  totalActive: number;
}

export async function curateRules(): Promise<CuratorReport> {
  const response = await ok(
    await fetch('/api/memory-engine/curate', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
    'memory-engine/curate',
  );
  const data = (await response.json()) as { report?: Record<string, unknown> };
  const r = data.report ?? {};
  const n = (k: string) => Number(r[k] ?? 0) || 0;
  return { deduped: n('deduped'), inverted: n('inverted'), promoted: n('promoted'), demoted: n('demoted'), pruned: n('pruned'), totalActive: n('total_active') };
}

export async function previewPack(query = ''): Promise<{ text: string; chars: number; budget: number; degraded: boolean }> {
  const data = await getJson<{ pack?: string; chars?: number; budget?: number; degraded?: boolean }>(`/api/memory-engine/pack?query=${encodeURIComponent(query)}`);
  return { text: data.pack ?? '', chars: data.chars ?? 0, budget: data.budget ?? 0, degraded: Boolean(data.degraded) };
}
