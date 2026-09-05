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

/* ── Running research from the Research screen ── */

export interface ResearchSettings {
  maxRounds: number;
  category: string;
  searchProvider: string;
  endpointId: string;
  model: string;
}

export interface ResearchProgress {
  phase: string;
  round?: number;
  queries?: number;
  totalSources?: number;
  totalFindings?: number;
  message?: string;
  status?: string;
}

export interface ResearchResult {
  result: string;
  sources: ResearchSource[];
  findings: string[];
  category: string;
}

async function postJson(path: string, body: unknown): Promise<Record<string, unknown>> {
  const res = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) {
    let detail = '';
    try {
      detail = String(((await res.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      /* not json */
    }
    throw new ApiError(detail || `${path} responded ${res.status}`, res.status);
  }
  return (await res.json()) as Record<string, unknown>;
}

export async function startResearch(query: string, s: ResearchSettings): Promise<string> {
  const body: Record<string, unknown> = { query, max_rounds: s.maxRounds };
  if (s.category) body.category = s.category;
  if (s.searchProvider) body.search_provider = s.searchProvider;
  if (s.endpointId) body.endpoint_id = s.endpointId;
  if (s.model) body.model = s.model;
  const out = await postJson('/api/research/start', body);
  return String(out.session_id ?? '');
}

export async function cancelResearch(id: string): Promise<void> {
  await post(`/api/research/cancel/${encodeURIComponent(id)}`);
}

function progressFrom(raw: Record<string, unknown>): ResearchProgress {
  return {
    phase: String(raw.phase ?? ''),
    round: typeof raw.round === 'number' ? raw.round : undefined,
    queries: typeof raw.queries === 'number' ? raw.queries : undefined,
    totalSources: typeof raw.total_sources === 'number' ? raw.total_sources : undefined,
    totalFindings: typeof raw.total_findings === 'number' ? raw.total_findings : undefined,
    message: typeof raw.message === 'string' ? raw.message : undefined,
    status: typeof raw.status === 'string' ? raw.status : undefined,
  };
}

export interface ActiveResearch {
  id: string;
  query: string;
  progress: ResearchProgress;
  startedAt: number;
}

export async function activeResearch(signal?: AbortSignal): Promise<ActiveResearch[]> {
  const raw = await getJson<{ active?: Record<string, unknown>[] }>('/api/research/active', signal);
  return (raw.active ?? []).map((a) => ({
    id: String(a.session_id ?? ''),
    query: String(a.query ?? ''),
    progress: progressFrom((a.progress && typeof a.progress === 'object' ? a.progress : {}) as Record<string, unknown>),
    startedAt: Number(a.started_at) || 0,
  }));
}

export async function researchStatus(id: string, signal?: AbortSignal): Promise<{ status: string; progress: ResearchProgress }> {
  const raw = await getJson<Record<string, unknown>>(`/api/research/status/${encodeURIComponent(id)}`, signal);
  return { status: String(raw.status ?? ''), progress: progressFrom((raw.progress && typeof raw.progress === 'object' ? raw.progress : {}) as Record<string, unknown>) };
}

/**
 * Follows a running job over SSE. Calls `onProgress` for every event and
 * resolves with the final status ("done", "error", "cancelled"…). Falls back
 * to polling when the stream cannot be opened.
 */
export function followResearch(id: string, onProgress: (p: ResearchProgress) => void, signal?: AbortSignal): Promise<string> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (status: string) => {
      if (settled) return;
      settled = true;
      resolve(status);
    };
    let source: EventSource | null = null;
    try {
      source = new EventSource(`/api/research/stream/${encodeURIComponent(id)}`);
    } catch {
      source = null;
    }
    const poll = async () => {
      while (!settled && !signal?.aborted) {
        try {
          const s = await researchStatus(id, signal);
          onProgress(s.progress);
          if (s.status && s.status !== 'running') return finish(s.status);
        } catch {
          return finish('error');
        }
        await new Promise((r) => window.setTimeout(r, 3000));
      }
    };
    if (!source) {
      void poll();
      return;
    }
    source.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as Record<string, unknown>;
        const p = progressFrom(data);
        if (typeof data.error === 'string' && data.error) onProgress({ ...p, phase: 'error', message: data.error });
        else onProgress(p);
        if (data.status === 'not_found') {
          source?.close();
          finish('error');
        } else if (p.status && p.status !== 'running') {
          source?.close();
          finish(p.status);
        }
      } catch {
        /* keep listening */
      }
    };
    source.onerror = () => {
      source?.close();
      void poll();
    };
    signal?.addEventListener('abort', () => {
      source?.close();
      finish('aborted');
    });
  });
}

function resultFrom(raw: Record<string, unknown>): ResearchResult {
  return {
    result: String(raw.result ?? ''),
    sources: asArray<Record<string, unknown>>(raw, 'sources').map((s) => ({ title: String(s.title ?? s.url ?? ''), url: typeof s.url === 'string' ? s.url : '' })),
    findings: asArray<unknown>(raw, 'raw_findings').map((f) => (typeof f === 'string' ? f : String((f as { text?: unknown })?.text ?? ''))).filter(Boolean),
    category: String(raw.category ?? ''),
  };
}

/** The finished report of a job this browser started (or any saved one, via peek). */
export async function researchResult(id: string): Promise<ResearchResult> {
  try {
    return resultFrom(await post(`/api/research/result/${encodeURIComponent(id)}`));
  } catch {
    return resultFrom(await post(`/api/research/result-peek/${encodeURIComponent(id)}`));
  }
}

export interface SearchProvider {
  id: string;
  label: string;
  available: boolean;
}

export async function searchProviders(signal?: AbortSignal): Promise<SearchProvider[]> {
  try {
    const raw = await getJson<unknown>('/api/search/providers', signal);
    return asArray<Record<string, unknown>>(raw).map((p) => ({ id: String(p.id ?? ''), label: String(p.label ?? p.id ?? ''), available: p.available !== false }));
  } catch {
    return [];
  }
}

export interface ResearchFit {
  tier: string;
  note: string;
  vramGb: number | null;
  ramGb: number | null;
  gpuName: string;
  changes: { key: string; from: unknown; to: unknown }[];
  alreadyApplied: boolean;
  blockers: { key: string; text: string; fixLabel: string; hasFix: boolean }[];
}

/** The profile the server recommends for this machine (the `/research-fit` command of the previous interface). */
export async function researchFit(signal?: AbortSignal): Promise<ResearchFit> {
  const raw = await getJson<Record<string, unknown>>('/api/research/preset', signal);
  return {
    tier: String(raw.tier ?? ''),
    note: String(raw.note ?? ''),
    vramGb: typeof raw.vram_gb === 'number' ? raw.vram_gb : null,
    ramGb: typeof raw.ram_gb === 'number' ? raw.ram_gb : null,
    gpuName: String(raw.gpu_name ?? ''),
    changes: asArray<Record<string, unknown>>(raw, 'changes').map((c) => ({ key: String(c.key ?? ''), from: c.from, to: c.to })),
    alreadyApplied: Boolean(raw.already_applied),
    blockers: asArray<Record<string, unknown>>(raw, 'blockers').map((b) => ({ key: String(b.key ?? ''), text: String(b.text ?? ''), fixLabel: String(b.fix_label ?? ''), hasFix: Boolean(b.fix) })),
  };
}

export async function applyResearchFit(includeFixes: boolean): Promise<{ tier: string; applied: string[] }> {
  const out = await postJson('/api/research/preset/apply', { include_fixes: includeFixes });
  const written = out.written && typeof out.written === 'object' ? Object.keys(out.written as Record<string, unknown>) : asArray<unknown>(out, 'written').map(String);
  return { tier: String(out.tier ?? ''), applied: written };
}

export const CATEGORIES: { value: string; label: string }[] = [
  { value: '', label: 'Auto' },
  { value: 'product', label: 'Product' },
  { value: 'comparison', label: 'Comparison' },
  { value: 'howto', label: 'How-to' },
  { value: 'factcheck', label: 'Fact-check' },
];

/** What the progress means, as one line. */
export function phaseLabel(p: ResearchProgress | null, maxRounds: number): string {
  if (!p || !p.phase) return t('Starting…');
  const round = p.round ? (maxRounds ? t('Round {n} of {m}: ', { n: p.round, m: maxRounds }) : t('Round {n}: ', { n: p.round })) : '';
  switch (p.phase) {
    case 'probing':
      return t('Probing the model…');
    case 'planning':
      return t('Planning the research…');
    case 'searching':
      return `${round}${t('Searching ({n} queries)', { n: p.queries ?? 0 })}`;
    case 'reading':
      return `${round}${t('Reading {n} sources', { n: p.totalSources ?? 0 })}`;
    case 'analyzing':
      return `${round}${t('Analysing {n} findings', { n: p.totalFindings ?? 0 })}`;
    case 'writing':
      return t('Writing the report — {n} sources', { n: p.totalSources ?? 0 });
    case 'error':
    case 'warning':
      return p.message || p.phase;
    default:
      return p.phase;
  }
}
