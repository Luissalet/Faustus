import { getJson } from './api';

/**
 * ADP-11 / CMP-05: `GET /api/attention` / `POST /api/attention/read`
 * (routes/attention_routes.py, `src/attention.py::classify`).
 *
 * A thin, typed wrapper — the actual merge into Activity's run list lives in
 * `adapters/activity.ts::mergeAttention`, next to the `ActivityRun` shape it
 * augments, so this file stays only "talk to the two endpoints".
 *
 * CMP-05 deepens ADP-11: `kind`/`reason`/`priority` stay exactly what they
 * were (the fixed-priority "what needs a person, in what order" word).
 * `lifecycle`, `waitCause`, `connectionHealth`/`signal` and `nextAction` are
 * NEW, independent facts about the same row — see `src/attention.py`'s
 * module docstring for what each one means and why they are kept apart
 * instead of being folded back into one word.
 */

export type AttentionKind =
  | 'approval' | 'question' | 'finished_unreviewed'
  | 'disconnected' | 'queued_model' | 'dependency';

/** The run's own machine state — independent of WHY it might be stuck. */
export type Lifecycle = 'queued' | 'running' | 'waiting' | 'finished' | 'failed' | 'cancelled';
/** Why it is not just proceeding right now, if anything. */
export type WaitCause = 'approval' | 'question' | 'gpu_queue' | 'dependency' | 'none';
export type ConnectionHealth = 'live' | 'stale' | 'disconnected';
/** `heuristic` is reserved for CMP-06's external-runtime adapter — nothing
 *  `src/attention.py` classifies today ever sets it (see its docstring). */
export type SignalSource = 'events' | 'heartbeat' | 'heuristic';
/** The one deterministic "what do you click" a card resolves to. */
export type NextAction = 'approve' | 'answer' | 'open' | 'retry' | 'reconnect';

export interface AttentionSignal {
  source: SignalSource;
  /** Seconds since the last real signal, or null when none was ever observed. */
  ageS: number | null;
  /** Epoch seconds of that signal, or null. */
  lastEventAt: number | null;
}

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
  lifecycle: Lifecycle;
  waitCause: WaitCause;
  connectionHealth: ConnectionHealth;
  signal: AttentionSignal;
  nextAction: NextAction;
  /** `Session.project_id`, when this row's session has one — null groups
   *  into "no project" in the studio's "by project" view. */
  projectId: string | null;
}

interface RawAttentionSignal {
  source?: string;
  age_s?: number | null;
  last_event_at?: number | null;
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
  lifecycle?: string;
  wait_cause?: string;
  connection_health?: string;
  signal?: RawAttentionSignal;
  next_action?: string;
  project_id?: string | null;
}

export interface AttentionFeed {
  rows: AttentionRow[];
  unreadCount: number;
}

const KNOWN_KINDS: AttentionKind[] = ['approval', 'question', 'finished_unreviewed', 'disconnected', 'queued_model', 'dependency'];
const KNOWN_LIFECYCLES: Lifecycle[] = ['queued', 'running', 'waiting', 'finished', 'failed', 'cancelled'];
const KNOWN_WAIT_CAUSES: WaitCause[] = ['approval', 'question', 'gpu_queue', 'dependency', 'none'];
const KNOWN_HEALTH: ConnectionHealth[] = ['live', 'stale', 'disconnected'];
const KNOWN_SOURCES: SignalSource[] = ['events', 'heartbeat', 'heuristic'];
const KNOWN_ACTIONS: NextAction[] = ['approve', 'answer', 'open', 'retry', 'reconnect'];

function oneOf<T extends string>(known: T[], value: unknown, fallback: T): T {
  return typeof value === 'string' && (known as string[]).includes(value) ? (value as T) : fallback;
}

function signalFrom(raw: RawAttentionSignal | undefined): AttentionSignal {
  return {
    source: oneOf(KNOWN_SOURCES, raw?.source, 'events'),
    ageS: typeof raw?.age_s === 'number' ? raw.age_s : null,
    lastEventAt: typeof raw?.last_event_at === 'number' ? raw.last_event_at : null,
  };
}

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
    lifecycle: oneOf(KNOWN_LIFECYCLES, raw.lifecycle, 'waiting'),
    waitCause: oneOf(KNOWN_WAIT_CAUSES, raw.wait_cause, 'none'),
    connectionHealth: oneOf(KNOWN_HEALTH, raw.connection_health, 'live'),
    signal: signalFrom(raw.signal),
    nextAction: oneOf(KNOWN_ACTIONS, raw.next_action, 'open'),
    projectId: typeof raw.project_id === 'string' && raw.project_id ? raw.project_id : null,
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
