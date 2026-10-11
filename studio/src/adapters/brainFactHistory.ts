import { asArray, getJson } from './api';

/**
 * Change chain of one fact (FAUSTUS fact_history / OBJ-60): the value in
 * force and the earlier ones with their validity window and the memory they
 * were learned from. Read-only over GET /api/brain/fact-history.
 */

export interface FactEvent {
  id: string;
  value: string;
  validFrom: string | null;
  validUntil: string | null;
  recordedAt: string | null;
  status: string;
  supersedes: string;
  supersededBy: string;
  /** First evidence ref ("mem:<id>" for a memory), or ''. */
  source: string;
  /** Session the source memory belongs to, or ''. */
  sourceSession: string;
}

export interface FactHistory {
  subjectId: string;
  subjectName: string;
  attribute: string;
  current: FactEvent[];
  /** Oldest first, as the server returns them. */
  events: FactEvent[];
  /** Earlier values, newest first (what the UI lists under the current one). */
  previous: FactEvent[];
  historyLine: string;
  changes: number;
}

function s(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function sOrNull(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

function eventFrom(raw: unknown): FactEvent {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  return {
    id: s(r.id),
    value: s(r.value),
    validFrom: sOrNull(r.valid_from),
    validUntil: sOrNull(r.valid_until),
    recordedAt: sOrNull(r.recorded_at),
    status: s(r.status) || 'active',
    supersedes: s(r.supersedes),
    supersededBy: s(r.superseded_by),
    source: s(r.source),
    sourceSession: s(r.source_session),
  };
}

export function factHistoryUrl(subject: string, attribute: string, limit = 20): string {
  return `/api/brain/fact-history?subject=${encodeURIComponent(subject)}&attribute=${encodeURIComponent(attribute)}&limit=${limit}`;
}

export function factHistoryFrom(raw: unknown): FactHistory {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const subject = (r.subject && typeof r.subject === 'object' ? r.subject : {}) as Record<string, unknown>;
  const current = asArray<unknown>(r.current).map(eventFrom);
  const events = asArray<unknown>(r.events).map(eventFrom);
  const currentIds = new Set(current.map((e) => e.id));
  const previous = events
    .filter((e) => !currentIds.has(e.id) && e.value)
    .sort((a, b) => (b.validUntil ?? b.recordedAt ?? '').localeCompare(a.validUntil ?? a.recordedAt ?? ''));
  return {
    subjectId: s(subject.id),
    subjectName: s(subject.name),
    attribute: s(r.attribute),
    current,
    events,
    previous,
    historyLine: s(r.history_line),
    changes: typeof r.changes === 'number' ? r.changes : 0,
  };
}

export async function loadFactHistory(subject: string, attribute: string, signal?: AbortSignal): Promise<FactHistory> {
  return factHistoryFrom(await getJson<unknown>(factHistoryUrl(subject, attribute), signal));
}

/** "mem:<id>" is a memory; the session link is the only deep link we have. */
export function sourceHref(event: FactEvent): string | null {
  return event.sourceSession ? `/studio?s=${encodeURIComponent(event.sourceSession)}` : null;
}
