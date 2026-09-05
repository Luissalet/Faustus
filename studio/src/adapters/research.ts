import { ApiError, asArray, getJson } from './api';
import { t } from '../i18n';

/**
 * Deep Research reports (routes/research): the library of finished reports,
 * their detail (summary + sources), exports, and the "discuss" spin-off
 * that seeds a chat with the report.
 */

export interface ResearchItem {
  id: string;
  query: string;
  category: string;
  sourceCount: number;
  status: string;
  duration: string;
  rounds: string;
  startedAt: number;
  completedAt: number;
  archived: boolean;
  thumbnail: string | null;
}

export interface ResearchSource {
  title: string;
  url: string;
}

export interface ResearchDetail {
  summary: string;
  report: string;
  sources: ResearchSource[];
  stats: Record<string, string>;
}

export type ResearchSort = 'recent' | 'oldest' | 'most-sources' | 'alpha';

function itemFrom(raw: Record<string, unknown>): ResearchItem {
  return {
    id: String(raw.id ?? ''),
    query: String(raw.query ?? ''),
    category: String(raw.category ?? ''),
    sourceCount: Number(raw.source_count) || 0,
    status: String(raw.status ?? 'done'),
    duration: String(raw.duration ?? ''),
    rounds: String(raw.rounds ?? ''),
    startedAt: Number(raw.started_at) || 0,
    completedAt: Number(raw.completed_at) || 0,
    archived: Boolean(raw.archived),
    thumbnail: typeof raw.thumbnail === 'string' && raw.thumbnail ? raw.thumbnail : null,
  };
}

export async function loadResearchLibrary(query: { search?: string; sort?: ResearchSort; archived?: boolean; limit?: number } = {}, signal?: AbortSignal): Promise<{ items: ResearchItem[]; total: number }> {
  const q = new URLSearchParams();
  if (query.search) q.set('search', query.search);
  q.set('sort', query.sort ?? 'recent');
  q.set('limit', String(query.limit ?? 50));
  if (query.archived) q.set('archived', 'true');
  const raw = await getJson<Record<string, unknown>>(`/api/research/library?${q}`, signal);
  return { items: asArray<Record<string, unknown>>(raw, 'research').map(itemFrom), total: typeof raw.total === 'number' ? raw.total : 0 };
}

export async function researchDetail(id: string): Promise<ResearchDetail> {
  const raw = await getJson<Record<string, unknown>>(`/api/research/detail/${encodeURIComponent(id)}`);
  const sources = asArray<Record<string, unknown>>(raw, 'sources').map((s) => ({ title: String(s.title ?? s.url ?? ''), url: typeof s.url === 'string' ? s.url : '' }));
  const stats: Record<string, string> = {};
  if (raw.stats && typeof raw.stats === 'object') for (const [k, v] of Object.entries(raw.stats as Record<string, unknown>)) stats[k] = String(v ?? '');
  return {
    summary: String(raw.summary ?? raw.report_summary ?? ''),
    report: String(raw.result ?? raw.raw_report ?? ''),
    sources,
    stats,
  };
}

async function post(path: string): Promise<Record<string, unknown>> {
  const res = await fetch(path, { method: 'POST', credentials: 'same-origin' });
  if (!res.ok) {
    let detail = '';
    try {
      detail = String(((await res.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      /* not json */
    }
    throw new ApiError(detail || `${path} responded ${res.status}`, res.status);
  }
  try {
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export async function setResearchArchived(id: string, archived: boolean): Promise<void> {
  await post(`/api/research/${encodeURIComponent(id)}/archive?archived=${archived ? 'true' : 'false'}`);
}

export async function deleteResearch(id: string): Promise<void> {
  const res = await fetch(`/api/research/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' });
  if (!res.ok) throw new ApiError(t('The report could not be deleted'), res.status);
}

/** A new chat seeded with the report and its sources. Returns the session id. */
export async function discussResearch(id: string): Promise<{ sessionId: string; name: string }> {
  const out = await post(`/api/research/spinoff/${encodeURIComponent(id)}`);
  return { sessionId: String(out.session_id ?? ''), name: String(out.name ?? '') };
}

export const reportUrl = (id: string) => `/api/research/report/${encodeURIComponent(id)}`;
export const exportUrl = (id: string, format: string) => `/api/research/export/${encodeURIComponent(id)}?format=${encodeURIComponent(format)}`;

export async function exportFormats(): Promise<string[]> {
  try {
    const raw = await getJson<{ formats?: unknown }>('/api/research/export-formats');
    return asArray<unknown>(raw.formats).map(String);
  } catch {
    return ['md'];
  }
}
