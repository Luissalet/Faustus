import { ApiError, getJson } from './api';

/**
 * The bundled skill library (`src/skill_library.py`,
 * `routes/skill_library_routes.py`) and the hybrid skill selector
 * (`src/skills_runtime/selector.py`, `routes/skill_selector_routes.py`).
 * Two small, related things: what a fresh install can add to the skill
 * store, and how the running selector would rank a query against what is
 * already there.
 */

export interface LibrarySkill {
  slug: string;
  name?: string;
  description?: string;
  category?: string;
  tags?: string[];
  when_to_use?: string;
  words?: number;
  installed?: boolean;
  error?: string;
}

async function readError(response: Response, path: string): Promise<string> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown; error?: unknown };
    if (typeof body?.detail === 'string' && body.detail.trim()) return body.detail;
    if (typeof body?.error === 'string' && body.error.trim()) return body.error;
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

/** The route's own field is `installed_for` (present only for a signed-in
 * owner); normalized here to the plain `installed` boolean callers want. */
function librarySkillFrom(raw: Record<string, unknown>): LibrarySkill {
  return {
    slug: String(raw.slug ?? ''),
    name: typeof raw.name === 'string' ? raw.name : undefined,
    description: typeof raw.description === 'string' ? raw.description : undefined,
    category: typeof raw.category === 'string' ? raw.category : undefined,
    tags: Array.isArray(raw.tags) ? raw.tags.map(String) : undefined,
    when_to_use: typeof raw.when_to_use === 'string' ? raw.when_to_use : undefined,
    words: typeof raw.words === 'number' ? raw.words : undefined,
    installed: Boolean(raw.installed_for),
    error: typeof raw.error === 'string' ? raw.error : undefined,
  };
}

export async function listSkillLibrary(): Promise<LibrarySkill[]> {
  const data = await getJson<{ skills: Record<string, unknown>[]; count: number }>('/api/skills/library');
  return (data.skills ?? []).map(librarySkillFrom);
}

export interface InstallResultRow {
  slug: string;
  status: 'installed' | 'skipped' | 'refused' | 'not_found' | 'error';
  name?: string;
  reason?: string;
}

export function installLibrarySkills(slugs: string[], replace = false): Promise<{ results: InstallResultRow[] }> {
  return postJson('/api/skills/library/install', { slugs, replace });
}

export interface UninstallResultRow {
  slug: string;
  name?: string;
  status: 'removed' | 'not_installed' | 'error';
}

export function uninstallLibrarySkills(slugs: string[]): Promise<{ results: UninstallResultRow[] }> {
  return postJson('/api/skills/library/uninstall', { slugs });
}

/* ── Selector ── */

export interface SelectorBreakdown {
  semantic: number;
  lexical: number;
  trigger: number;
  prior: number;
  score: number;
}

export interface SelectorCandidate {
  name: string;
  description?: string;
  status?: string;
  confidence?: number;
  _selector: SelectorBreakdown;
}

function candidateFrom(raw: Record<string, unknown>): SelectorCandidate {
  const sel = (raw._selector && typeof raw._selector === 'object' ? raw._selector : {}) as Record<string, unknown>;
  return {
    name: String(raw.name ?? ''),
    description: typeof raw.description === 'string' ? raw.description : undefined,
    status: typeof raw.status === 'string' ? raw.status : undefined,
    confidence: typeof raw.confidence === 'number' ? raw.confidence : undefined,
    _selector: {
      semantic: Number(sel.semantic) || 0,
      lexical: Number(sel.lexical) || 0,
      trigger: Number(sel.trigger) || 0,
      prior: Number(sel.prior) || 0,
      score: Number(sel.score) || 0,
    },
  };
}

export async function explainSelector(query: string, maxItems = 20): Promise<SelectorCandidate[]> {
  const data = await postJson<{ candidates: Record<string, unknown>[] }>('/api/skills/selector/explain', { query, max_items: maxItems });
  return (data.candidates ?? []).map(candidateFrom);
}

export type SelectorMode = 'hybrid' | 'lexical';

export interface SelectorSettings {
  mode: SelectorMode;
  threshold: number;
  weights: { semantic: number; lexical: number; trigger: number };
}

export function getSelectorSettings(): Promise<SelectorSettings> {
  return getJson<SelectorSettings>('/api/skills/selector');
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await readError(response, path), response.status);
  return (await response.json()) as T;
}

export function saveSelectorSettings(patch: Partial<SelectorSettings>): Promise<SelectorSettings> {
  return putJson<SelectorSettings>('/api/skills/selector', patch);
}
