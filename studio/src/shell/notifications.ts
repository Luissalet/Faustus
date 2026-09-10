import { ApiError, asArray, getJson, responseReason } from '../adapters/api';
import type { ActivityRun } from '../adapters/activity';

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
 */

export type NotificationType = 'approval' | 'question' | 'task_done' | 'task_failed' | 'blocked';

export interface NotificationChannelPrefs {
  enabled: boolean;
  channels: ('in_app' | 'desktop')[];
}

export interface NotificationPrefs {
  channels: { in_app: boolean; desktop: boolean };
  types: Record<NotificationType, NotificationChannelPrefs>;
  quiet_hours: { enabled: boolean; start: string; end: string };
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

export async function putNotificationPrefs(patch: Partial<NotificationPrefs>): Promise<NotificationPrefs> {
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
    return { type: 'task_failed', dedupe_key: dedupeKeyForRun(run), title: run.title, detail: run.error ?? run.detail ?? '', screen: 'activity', target };
  }
  if (run.kind === 'task' && run.status === 'succeeded') {
    return { type: 'task_done', dedupe_key: dedupeKeyForRun(run), title: run.title, detail: run.detail ?? '', screen: 'activity', target };
  }
  return null;
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
