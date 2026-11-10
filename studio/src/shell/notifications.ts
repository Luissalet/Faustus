import { ApiError, asArray, getJson, responseReason } from '../adapters/api';
import type { ActivityRun } from '../adapters/activity';
import type { PersonInbox } from '../adapters/agora';
import { t } from '../i18n';

/**
 * ACT-02: notification preferences, dedupe and the in-app tray's feed
 * (routes/notifications_routes.py).
 *
 * Nothing on the server watches for events on its own — this module is the
 * thing that does: `Activity.tsx`'s poller already learns about every new
 * pending approval/question and every task that just finished or failed, so
 * `emitForNewRuns` (called from there) is where a change becomes a stored
 * notification. `POST /emit` is idempotent per `dedupeKeyForRun`, so calling
 * it again for a run the tray already knows about — which a reconnect's
 * fresh poll does constantly — is a no-op rather than twenty duplicate
 * alerts for the same still-pending approval.
 *
 * The Agora is watched the same way: the tray reads what waits for the person
 * in the hub (a message that names them, a question the agents put to them)
 * and `emitForAgora` makes each one a notification, once. A phone with no
 * page open is told by the server instead (src/agora_watch.py). A server the
 * person silenced is left out by the hub, and a kind they switched off
 * (mentions, questions) is refused by `POST /emit` itself.
 */

export type NotificationType = 'approval' | 'question' | 'task_done' | 'task_failed' | 'blocked' | 'agora';

export interface NotificationChannelPrefs {
  enabled: boolean;
  channels: ('in_app' | 'desktop')[];
}

export interface NotificationPrefs {
  channels: { in_app: boolean; desktop: boolean };
  types: Record<NotificationType, NotificationChannelPrefs>;
  quiet_hours: { enabled: boolean; start: string; end: string };
  /** Which Agora news are told: a message that names the person, a question the agents put to them. */
  agora: { mentions: boolean; questions: boolean };
}

export interface StoredNotification {
  id: string;
  type: NotificationType;
  dedupe_key: string;
  title: string;
  detail: string;
  target: { screen: string; run: string };
  created_at: string;
  read: boolean;
  desktop_allowed: boolean;
  suppressed_quiet_hours: boolean;
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as T;
}

export async function getNotificationPrefs(signal?: AbortSignal): Promise<NotificationPrefs> {
  return getJson<NotificationPrefs>('/api/notifications/prefs', signal);
}

/** Only what is sent changes; of `agora`, only the switches sent. */
export async function putNotificationPrefs(patch: Partial<Omit<NotificationPrefs, 'agora'>> & { agora?: Partial<NotificationPrefs['agora']> }): Promise<NotificationPrefs> {
  return send<NotificationPrefs>('/api/notifications/prefs', 'PUT', patch);
}

export async function listNotifications(unreadOnly = false, signal?: AbortSignal): Promise<{ notifications: StoredNotification[]; unread: number }> {
  const data = await getJson<{ notifications?: StoredNotification[]; unread?: number }>(
    `/api/notifications?unread_only=${unreadOnly ? 'true' : 'false'}`, signal,
  );
  return { notifications: asArray(data.notifications), unread: data.unread ?? 0 };
}

export async function markNotificationRead(id: string): Promise<void> {
  await send(`/api/notifications/${encodeURIComponent(id)}/read`, 'POST');
}

export async function markAllNotificationsRead(): Promise<void> {
  await send('/api/notifications/read-all', 'POST');
}

export async function dismissNotification(id: string): Promise<void> {
  await send(`/api/notifications/${encodeURIComponent(id)}`, 'DELETE');
}

interface EmitPayload {
  type: NotificationType;
  dedupe_key: string;
  title: string;
  detail: string;
  screen: string;
  target: string;
}

async function emit(payload: EmitPayload): Promise<StoredNotification | null> {
  try {
    const data = await send<{ stored: boolean; notification?: StoredNotification }>('/api/notifications/emit', 'POST', payload);
    return data.notification ?? null;
  } catch {
    return null; // best-effort: a failed emit must never break the activity poll it rides on
  }
}

/** A stable key per run: same shape the server also uses to dedupe, so a
 *  poll tick that sees the same waiting approval again never re-alerts. */
export function dedupeKeyForRun(run: ActivityRun): string {
  return `${run.kind}:${run.id}`;
}

/** Housekeeping tasks whose success is not news: the tray filled up with
 *  "Chat Sessions Tidy — Cleaned 0 sessions (folder sort skipped)." three
 *  times in a row. They still notify when they fail. */
const QUIET_WHEN_DONE = new Set([
  'tidy_sessions', 'tidy_documents', 'tidy_research', 'consolidate_memory',
  'test_skills', 'audit_skills', 'learn_sender_signatures',
]);

/** A task result as one line of plain text: the tray and the desktop
 *  notification show `detail` as text, so Markdown marks (`**`, `###`,
 *  backticks, links) read as noise ("**Tiempo en Móstoles** — …"). */
export function plainDetail(text: string): string {
  return (text || '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/(^|\s)#{1,6}\s+/g, '$1')
    .replace(/\*\*|__|`+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Which runs are worth a notification, and what to say about them. Returns
 *  null for a run this feature has no opinion about (a running task, say). */
export function payloadForRun(run: ActivityRun): EmitPayload | null {
  const target = `${run.kind}-${run.id}`;
  if (run.kind === 'approval' && run.status === 'waiting') {
    return { type: 'approval', dedupe_key: dedupeKeyForRun(run), title: run.title, detail: run.detail ?? '', screen: 'activity', target };
  }
  if (run.kind === 'question' && run.status === 'waiting') {
    return { type: 'question', dedupe_key: dedupeKeyForRun(run), title: run.title, detail: run.detail ?? '', screen: 'activity', target };
  }
  if (run.kind === 'task' && run.status === 'failed') {
    return { type: 'task_failed', dedupe_key: dedupeKeyForRun(run), title: run.title, detail: plainDetail(run.error ?? run.detail ?? ''), screen: 'activity', target };
  }
  if (run.kind === 'task' && run.status === 'succeeded') {
    if (QUIET_WHEN_DONE.has(run.task?.action ?? '')) return null;
    return { type: 'task_done', dedupe_key: dedupeKeyForRun(run), title: run.title, detail: plainDetail(run.detail ?? ''), screen: 'activity', target };
  }
  return null;
}

/** Older than this, what waits in the Agora is history: it stays in the Agora's own inbox and nobody is told again. */
const AGORA_FRESH_S = 86400;

/** In one look, at most this many become notifications (the latest); the rest stay in the Agora's own inbox. */
const AGORA_PER_LOOK = 5;

const agoraPlace = (space: string, thread: number) => `/agora?space_id=${encodeURIComponent(space)}&c=${encodeURIComponent('t:' + thread)}`;

/** A question asked again in other words is a new question: its key carries what was asked. */
function questionKey(q: { space_id: string; id: number; question: string | null }): string {
  let hash = 5381;
  for (const ch of q.question ?? '') hash = ((hash * 33) ^ ch.charCodeAt(0)) >>> 0;
  return `agora:q:${q.space_id}:${q.id}:${hash.toString(36)}`;
}

/** The key of everything that waits for the person in the Agora right now, fresh or not: a stored notification
 *  whose key is no longer here was answered or read where it was said. */
export function agoraWaitingKeys(inbox: PersonInbox): Set<string> {
  return new Set([
    ...inbox.escalated.map(questionKey),
    ...inbox.pending.map((m) => `agora:m:${m.space_id}:${m.id}`),
  ]);
}

/** What in the person's Agora inbox is worth a notification: a question put to them and a message that names them. */
export function payloadsForInbox(inbox: PersonInbox, now = Date.now() / 1000): EmitPayload[] {
  const out: EmitPayload[] = [];
  for (const q of inbox.escalated) {
    if (now - q.updated > AGORA_FRESH_S) continue;
    out.push({
      type: 'agora', dedupe_key: questionKey(q),
      title: q.escalated_by ? t('{name} asks you: {title}', { name: q.escalated_by, title: q.title }) : q.title,
      detail: plainDetail(q.question ?? '').slice(0, 400), screen: 'agora', target: agoraPlace(q.space_id, q.id),
    });
  }
  // A question put to the person also names them: it is told once, as the question.
  const asked = new Set(inbox.escalated.map((q) => q.space_id + ':' + q.id));
  for (const m of inbox.pending) {
    if (now - m.created > AGORA_FRESH_S || asked.has(m.space_id + ':' + m.thread_id)) continue;
    out.push({
      type: 'agora', dedupe_key: `agora:m:${m.space_id}:${m.id}`,
      title: t('{name} mentions you in {where}', { name: m.author, where: (m.task_id ? '#' + m.task_id + ' ' : '') + m.thread_title }),
      detail: plainDetail(m.body).slice(0, 400), screen: 'agora', target: agoraPlace(m.space_id, m.thread_id),
    });
  }
  return out;
}

/** One notification per thing that waits in the Agora and this page has not already reported. After a long
 *  absence only the latest few are told; the others are taken as seen and wait in the Agora's inbox. */
export async function emitForAgora(inbox: PersonInbox, seen: Set<string>): Promise<void> {
  const when = new Map<string, number>([
    ...inbox.escalated.map((q) => [questionKey(q), q.updated] as [string, number]),
    ...inbox.pending.map((m) => [`agora:m:${m.space_id}:${m.id}`, m.created] as [string, number]),
  ]);
  const fresh = payloadsForInbox(inbox).filter((payload) => !seen.has(payload.dedupe_key))
    .sort((a, b) => (when.get(b.dedupe_key) ?? 0) - (when.get(a.dedupe_key) ?? 0));
  for (const payload of fresh) seen.add(payload.dedupe_key);
  for (const payload of fresh.slice(0, AGORA_PER_LOOK).reverse()) await emit(payload);
}

/** Emits one notification per run this poll tick has not already reported,
 *  tracked in `seen` (a Set the caller owns across polls — Activity.tsx
 *  keeps it in a ref so it survives re-renders but resets on remount). */
export async function emitForNewRuns(runs: ActivityRun[], seen: Set<string>): Promise<void> {
  const fresh = runs
    .map(payloadForRun)
    .filter((p): p is EmitPayload => p !== null && !seen.has(p.dedupe_key));
  for (const payload of fresh) {
    seen.add(payload.dedupe_key);
    await emit(payload);
  }
}
