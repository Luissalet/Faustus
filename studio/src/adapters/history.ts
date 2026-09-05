/**
 * Imported history (`/api/history-import`): somebody else's export — a
 * ChatGPT or Claude `conversations.json`, an LM Studio chat folder, one of
 * this app's own JSON exports — previewed first, then written, then a
 * normal searchable part of the library.
 */
import { ApiError, asArray, getJson } from './api';

export const SOURCES = ['chatgpt', 'claude', 'lmstudio', 'faustus'] as const;
export const SOURCE_LABEL: Record<string, string> = { chatgpt: 'ChatGPT', claude: 'Claude', lmstudio: 'LM Studio', faustus: 'Faustus' };

export interface Conversation {
  id: string;
  source: string;
  externalId: string;
  title: string;
  /** null stays null: "we do not know when this was said". */
  startedAt: string | null;
  endedAt: string | null;
  model: string;
  messageCount: number;
  importedAt: string;
  path: string;
}

export interface HistoryMessage {
  id: string;
  role: string;
  content: string;
  ts: string | null;
  ordinal: number;
}

export interface ConversationDetail extends Conversation {
  messages: HistoryMessage[];
}

export interface HistoryStats {
  conversations: number;
  messages: number;
  oldest: string | null;
  newest: string | null;
  sources: { source: string; conversations: number; messages: number }[];
  enabled: boolean;
}

export interface ImportReport {
  detected: string;
  files: number;
  conversations: number;
  messages: number;
  created: number;
  updated: number;
  skipped: { why: string; where: string }[];
  dryRun: boolean;
  seconds: number;
}

export interface SearchHit {
  messageId: string;
  conversationId: string;
  title: string;
  source: string;
  role: string;
  ts: string | null;
  snippet: string;
  matchStart: number;
  matchEnd: number;
  score: number | null;
}

export interface SearchResult {
  hits: SearchHit[];
  tier: string;
  degraded: boolean;
  elapsedMs: number;
  candidates: number;
}

const str = (v: unknown): string => (v === null || v === undefined ? '' : String(v));
const n0 = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0);
const nullable = (v: unknown): string | null => (v ? String(v) : null);

export function sourceLabel(v: string): string {
  const key = v.trim().toLowerCase();
  return key ? (SOURCE_LABEL[key] ?? key) : 'unknown';
}

/** Only an ISO date is a date: a conversation with no date NEVER gets today's. */
export function dateLabel(value: string | null, unknown = 'date unknown'): string {
  const raw = (value ?? '').trim();
  if (!/^\d{4}-\d{2}-\d{2}/.test(raw)) return unknown;
  const when = new Date(raw);
  if (Number.isNaN(when.getTime())) return unknown;
  return when.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

function conversationFrom(raw: Record<string, unknown>): Conversation {
  return { id: str(raw.id), source: str(raw.source), externalId: str(raw.external_id), title: str(raw.title), startedAt: nullable(raw.started_at), endedAt: nullable(raw.ended_at), model: str(raw.model), messageCount: n0(raw.message_count), importedAt: str(raw.imported_at), path: str(raw.path) };
}

function statsFrom(raw: Record<string, unknown>): HistoryStats {
  return {
    conversations: n0(raw.conversations),
    messages: n0(raw.messages),
    oldest: nullable(raw.oldest),
    newest: nullable(raw.newest),
    sources: asArray<Record<string, unknown>>(raw.sources).map((s) => ({ source: str(s.source), conversations: n0(s.conversations), messages: n0(s.messages) })),
    enabled: raw.enabled !== false,
  };
}

async function send(path: string, init: RequestInit): Promise<Record<string, unknown>> {
  const res = await fetch(path, { credentials: 'same-origin', ...init });
  if (!res.ok) {
    let detail = '';
    try {
      detail = str(((await res.json()) as { detail?: unknown }).detail);
    } catch {
      /* not json */
    }
    throw new ApiError(detail || `${path} responded ${res.status}`, res.status);
  }
  return (await res.json()) as Record<string, unknown>;
}

export async function listConversations(opts: { source?: string; q?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal): Promise<{ conversations: Conversation[]; stats: HistoryStats; enabled: boolean }> {
  const p = new URLSearchParams();
  if (opts.source) p.set('source', opts.source);
  if (opts.q) p.set('q', opts.q);
  p.set('limit', String(opts.limit ?? 200));
  if (opts.offset) p.set('offset', String(opts.offset));
  const raw = await getJson<Record<string, unknown>>(`/api/history-import/conversations?${p.toString()}`, signal);
  const stats = statsFrom((raw.stats && typeof raw.stats === 'object' ? raw.stats : {}) as Record<string, unknown>);
  return { conversations: asArray<Record<string, unknown>>(raw.conversations).map(conversationFrom).filter((c) => c.id), stats, enabled: raw.enabled !== false };
}

export async function readConversation(id: string, signal?: AbortSignal): Promise<ConversationDetail> {
  const raw = await getJson<Record<string, unknown>>(`/api/history-import/conversations/${encodeURIComponent(id)}`, signal);
  const conv = (raw.conversation && typeof raw.conversation === 'object' ? raw.conversation : {}) as Record<string, unknown>;
  return {
    ...conversationFrom(conv),
    messages: asArray<Record<string, unknown>>(conv.messages).map((m) => ({ id: str(m.id), role: str(m.role), content: str(m.content), ts: nullable(m.ts), ordinal: n0(m.ordinal) })),
  };
}

export async function deleteConversation(id: string): Promise<void> {
  await send(`/api/history-import/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function historyStats(signal?: AbortSignal): Promise<HistoryStats> {
  return statsFrom(await getJson<Record<string, unknown>>('/api/history-import/stats', signal));
}

function reportFrom(raw: Record<string, unknown>): ImportReport {
  return {
    detected: str(raw.detected),
    files: n0(raw.files),
    conversations: n0(raw.conversations),
    messages: n0(raw.messages),
    created: n0(raw.created),
    updated: n0(raw.updated),
    skipped: asArray<Record<string, unknown>>(raw.skipped).map((s) => ({ why: str(s.why), where: str(s.where) })),
    dryRun: Boolean(raw.dry_run),
    seconds: n0(raw.seconds),
  };
}

/** Import a path on the server's machine. `dryRun` writes nothing. */
export async function importPath(path: string, source: string, dryRun: boolean): Promise<ImportReport> {
  return reportFrom(await send('/api/history-import/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, source: source || undefined, dry_run: dryRun }) }));
}

/** Upload an export file. A dry run deletes its upload again. */
export async function importUpload(file: File, source: string, dryRun: boolean): Promise<ImportReport> {
  const fd = new FormData();
  fd.append('file', file, file.name);
  if (source) fd.append('source', source);
  fd.append('dry_run', dryRun ? '1' : '0');
  return reportFrom(await send('/api/history-import/import', { method: 'POST', body: fd }));
}

export async function searchHistory(q: string, source?: string, k = 20, signal?: AbortSignal): Promise<SearchResult> {
  const p = new URLSearchParams({ q, k: String(k) });
  if (source) p.set('source', source);
  const raw = await getJson<Record<string, unknown>>(`/api/history-import/search?${p.toString()}`, signal);
  return {
    hits: asArray<Record<string, unknown>>(raw.hits).map((h) => ({
      messageId: str(h.message_id),
      conversationId: str(h.conversation_id),
      title: str(h.title),
      source: str(h.source),
      role: str(h.role),
      ts: nullable(h.ts),
      snippet: str(h.snippet),
      matchStart: n0(h.match_start),
      matchEnd: n0(h.match_end),
      score: h.score === null || h.score === undefined ? null : Number(h.score),
    })),
    tier: str(raw.tier) || 'lexical',
    degraded: Boolean(raw.degraded),
    elapsedMs: n0(raw.elapsed_ms),
    candidates: n0(raw.candidates),
  };
}
