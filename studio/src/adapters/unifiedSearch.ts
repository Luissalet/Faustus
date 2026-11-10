import { getJson } from './api';

/** One query across every kind of owned thing (`GET /api/search/all`,
 *  src/unified_search.py): chats, brain, notes, documents, gallery, skills,
 *  board issues, projects and agent settings, mixed by rank. */
export const SEARCH_SOURCES = ['chats', 'brain', 'notes', 'documents', 'gallery', 'skills', 'board', 'projects', 'settings'] as const;
export type SearchSource = (typeof SEARCH_SOURCES)[number];

/** `/chats contrato` searches only chats for «contrato»; `/` or `/pro` lists
 *  the scopes that start that way. Anything else (no slash first, or an
 *  unknown word followed by text) is an ordinary query. */
export type ScopeCommand =
  | { kind: 'list'; scopes: SearchSource[] }
  | { kind: 'run'; scope: SearchSource; rest: string };

/** Spanish words for the scopes (`/proyectos`, `/ajustes`…), same targets. */
export const SCOPE_ALIASES: Record<string, SearchSource> = {
  conversaciones: 'chats', cerebro: 'brain', notas: 'notes', documentos: 'documents',
  galeria: 'gallery', 'galería': 'gallery', habilidades: 'skills', tablero: 'board',
  proyectos: 'projects', ajustes: 'settings', opciones: 'settings',
};

function scopeWords(): Array<[string, SearchSource]> {
  return [...SEARCH_SOURCES.map((s) => [s, s] as [string, SearchSource]), ...Object.entries(SCOPE_ALIASES)];
}

export function parseScopeCommand(query: string): ScopeCommand | null {
  const m = /^\/(\S*)(?:\s+([\s\S]*))?$/.exec(query);
  if (!m) return null;
  const word = (m[1] ?? '').toLowerCase();
  if (m[2] !== undefined) {
    const hit = scopeWords().find(([w]) => w === word);
    return hit ? { kind: 'run', scope: hit[1], rest: m[2] } : null;
  }
  const scopes = [...new Set(scopeWords().filter(([w]) => w.startsWith(word)).map(([, s]) => s))];
  return scopes.length ? { kind: 'list', scopes } : null;
}

/** Character ranges of `text` that match the query's tokens (case-insensitive),
 *  merged and sorted, for highlighting a hit's title. */
export function matchRanges(text: string, query: string): Array<[number, number]> {
  const lower = text.toLowerCase();
  const ranges: Array<[number, number]> = [];
  for (const tok of query.toLowerCase().split(/\s+/).filter(Boolean)) {
    let from = 0;
    for (;;) {
      const at = lower.indexOf(tok, from);
      if (at < 0) break;
      ranges.push([at, at + tok.length]);
      from = at + tok.length;
    }
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: Array<[number, number]> = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  return merged;
}

export interface SearchHit {
  type: SearchSource;
  id: string;
  title: string;
  snippet: string;
  url: string;
  when: string | null;
  score: number;
}

export interface SearchAll {
  query: string;
  results: SearchHit[];
  counts: Partial<Record<SearchSource, number>>;
  errors: Partial<Record<SearchSource, string>>;
  elapsed_ms: number;
}

/** Time bounds for chat transcripts: `after` (from this date on) and
 *  `before` (as of: only what had been said by then), ISO dates. */
export interface SearchWhen { after?: string; before?: string }

export function searchAll(q: string, types: SearchSource[] = [], limit = 6, signal?: AbortSignal, when: SearchWhen = {}): Promise<SearchAll> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  if (types.length) params.set('types', types.join(','));
  if (when.after) params.set('after', when.after);
  if (when.before) params.set('before', when.before);
  return getJson<SearchAll>(`/api/search/all?${params.toString()}`, signal);
}
