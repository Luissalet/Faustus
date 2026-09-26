import { getJson } from './api';

/** One query across every kind of owned thing (`GET /api/search/all`,
 *  src/unified_search.py): chats, brain, notes, documents, gallery, skills,
 *  board issues, mixed by rank. */
export const SEARCH_SOURCES = ['chats', 'brain', 'notes', 'documents', 'gallery', 'skills', 'board'] as const;
export type SearchSource = (typeof SEARCH_SOURCES)[number];

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

export function searchAll(q: string, types: SearchSource[] = [], limit = 6, signal?: AbortSignal): Promise<SearchAll> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  if (types.length) params.set('types', types.join(','));
  return getJson<SearchAll>(`/api/search/all?${params.toString()}`, signal);
}
