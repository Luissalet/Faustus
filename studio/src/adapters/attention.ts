import { getJson } from './api';

/**
 * ADP-11: `GET /api/attention` / `POST /api/attention/read`
 * (routes/attention_routes.py, `src/attention.py::classify`).
 *
 * A thin, typed wrapper — the actual merge into Activity's run list lives in
 * `adapters/activity.ts::mergeAttention`, next to the `ActivityRun` shape it
 * augments, so this file stays only "talk to the two endpoints".
 */

export type AttentionKind =
  | 'approval' | 'question' | 'finished_unreviewed'
  | 'disconnected' | 'queued_model' | 'dependency';

export interface AttentionRow {
  sessionId: string;
  kind: AttentionKind;
  /** English source string (see src/attention.py's `_REASONS`) — translate with `t()` at render time, same as every other Activity label. */
  reason: string;
  priority: number;
  /** Epoch seconds the state began, or null when the source could not establish one. */
  since: number | null;
  /** Extra context (a queue position, a dependency's label) — never required to make sense of `reason` alone. */
  detail: string;
  label: string;
  unread: boolean;
}

interface RawAttentionRow {
  session_id?: string;
  kind?: string;
  reason?: string;
  priority?: number;
  since?: number | null;
  detail?: string;
  label?: string;
  unread?: boolean;
}

export interface AttentionFeed {
  rows: AttentionRow[];
  unreadCount: number;
}

const KNOWN_KINDS: AttentionKind[] = ['approval', 'question', 'finished_unreviewed', 'disconnected', 'queued_model', 'dependency'];

function rowFrom(raw: RawAttentionRow): AttentionRow | null {
  const sessionId = typeof raw.session_id === 'string' ? raw.session_id : '';
  const kind = raw.kind as AttentionKind;
  if (!sessionId || !KNOWN_KINDS.includes(kind)) return null;
  return {
    sessionId,
    kind,
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    priority: typeof raw.priority === 'number' ? raw.priority : KNOWN_KINDS.length,
    since: typeof raw.since === 'number' ? raw.since : null,
    detail: typeof raw.detail === 'string' ? raw.detail : '',
    label: typeof raw.label === 'string' ? raw.label : '',
    unread: Boolean(raw.unread),
  };
}

export async function loadAttention(limit = 50, signal?: AbortSignal): Promise<AttentionFeed> {
  const body = await getJson<{ runs?: RawAttentionRow[]; unread_count?: number }>(
    `/api/attention?limit=${encodeURIComponent(String(limit))}`, signal,
  );
  const rows = (body.runs ?? []).map(rowFrom).filter((r): r is AttentionRow => r !== null);
  return { rows, unreadCount: typeof body.unread_count === 'number' ? body.unread_count : rows.filter((r) => r.unread).length };
}

/**
 * Opening a run marks it read (ACT-11's own criterion: this NEVER resolves
 * an approval or answers a question — it only silences the unread badge;
 * the underlying decision still goes through the routes it always did).
 * Best-effort: a failed mark just means the badge reappears next poll,
 * never a reason to interrupt opening the run.
 */
export async function markAttentionRead(sessionIds: string[]): Promise<void> {
  const ids = sessionIds.filter((id) => typeof id === 'string' && id.trim());
  if (!ids.length) return;
  try {
    await fetch('/api/attention/read', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_ids: ids }),
      signal: AbortSignal.timeout(20000),
    });
  } catch {
    /* best-effort, see docstring above */
  }
}
