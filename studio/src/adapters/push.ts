/**
 * Web Push (lot P-B): the browser half of `src/push.py` /
 * `routes/push_routes.py`. Registers this device's `PushSubscription` with
 * the server so it can receive real OS notifications — even with Studio's
 * tab closed — once installed as the PWA (`static/sw.js`'s `push` handler
 * is the other end of `sendTest`/the notification bus).
 */
import { ApiError, getJson } from './api';

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function ok(response: Response, what: string): Promise<Response> {
  if (response.ok) return response;
  let detail = '';
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === 'string') detail = body.detail;
  } catch {
    /* not JSON */
  }
  throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
}

async function postJson<T>(path: string, body: unknown, what: string): Promise<T> {
  const r = await ok(
    await fetch(path, { method: 'POST', credentials: 'same-origin', headers: JSON_HEADERS, body: JSON.stringify(body) }),
    what,
  );
  const text = await r.text();
  return (text ? JSON.parse(text) : {}) as T;
}

export interface PushSubscriptionRow {
  id: string;
  endpoint: string;
  device_name: string;
  created_at: number;
  last_ok: number | null;
  failures: number;
}

/** `PushManager.subscribe()`'s `applicationServerKey` wants raw bytes, not
 * the base64url string the server hands back. Returned as `BufferSource`
 * directly (not `Uint8Array`, whose `.buffer` type widened to
 * `ArrayBufferLike` and no longer satisfied that option under this repo's
 * TS lib target) since the only caller ever passes it straight through. */
function urlBase64ToUint8Array(base64String: string): BufferSource {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; ++i) output[i] = raw.charCodeAt(i);
  return output;
}

/** Whether this browser can even attempt Web Push — no point offering the
 * toggle on a browser (or a plain non-HTTPS/non-localhost origin) that
 * will only throw. */
export function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  );
}

export async function getVapidKey(): Promise<string> {
  const { key } = await getJson<{ key: string }>('/api/push/vapid-key');
  return key;
}

/** The `PushSubscription` already registered on THIS device's service
 * worker, if any — independent of whether the server still knows about it
 * (a server wipe or a manual "Remove" elsewhere leaves the browser
 * subscribed until `unsubscribeThisDevice()` runs). */
export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!isSupported()) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

/** Ask for notification permission (if not already decided), subscribe
 * this device's service worker to Web Push, and register the subscription
 * with the server under `label`. Throws with a human-readable reason on
 * any failure (permission denied, no service worker yet, …). */
export async function subscribeThisDevice(label: string): Promise<PushSubscriptionRow> {
  if (!isSupported()) throw new ApiError('This browser does not support push notifications.', 400);

  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new ApiError('Notification permission was not granted.', 400);
  }

  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    const key = await getVapidKey();
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
  }

  const { subscription: row } = await postJson<{ subscription: PushSubscriptionRow }>(
    '/api/push/subscribe',
    { subscription: subscription.toJSON(), device_name: label },
    'Subscribe',
  );
  return row;
}

/** Unsubscribes THIS device's service worker AND removes its row from the
 * server. Either half succeeding alone would leave a half-registered
 * device (still buzzing, or unable to re-subscribe cleanly), so both run
 * even if one already failed. */
export async function unsubscribeThisDevice(): Promise<void> {
  const subscription = await currentSubscription();
  if (!subscription) return;
  const endpoint = subscription.endpoint;
  try {
    await subscription.unsubscribe();
  } finally {
    await postJson('/api/push/unsubscribe', { endpoint }, 'Unsubscribe').catch(() => undefined);
  }
}

export async function listSubscriptions(): Promise<PushSubscriptionRow[]> {
  const { subscriptions } = await getJson<{ subscriptions: PushSubscriptionRow[] }>('/api/push/subscriptions');
  return subscriptions;
}

export async function removeSubscription(endpointOrId: string): Promise<void> {
  await postJson('/api/push/unsubscribe', { endpoint: endpointOrId }, 'Remove device');
}

export async function sendTest(): Promise<void> {
  await postJson('/api/push/test', {}, 'Send test notification');
}
