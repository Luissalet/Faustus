import { getJson } from './api';

/**
 * studio/src/adapters/google.ts — G3.3: the "Configure Google" setup
 * wizard's adapter. Talks to `routes/google_oauth_routes.py` (G3.2) — the
 * single OAuth client Google Calendar and Gmail OAuth share
 * (`src/google_oauth_client.py`, G3.1).
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function ok(r: Response, what: string): Promise<Response> {
  if (r.ok) return r;
  let msg = `${what}: HTTP ${r.status}`;
  try {
    const d = (await r.json()) as { detail?: unknown };
    if (typeof d.detail === 'string') msg = d.detail;
  } catch {
    /* not json */
  }
  throw new Error(msg);
}

async function json<T>(path: string, init: RequestInit, what: string): Promise<T> {
  const r = await ok(await fetch(path, { credentials: 'same-origin', ...init }), what);
  const text = await r.text();
  return (text ? JSON.parse(text) : {}) as T;
}

/** Fixed step identities — `GoogleOAuthSetup.tsx` supplies the (translated)
 * copy per key; the server only orders them. */
export type GoogleOAuthStep = 'project' | 'enable_calendar_api' | 'consent_screen' | 'test_user' | 'credentials' | 'paste';

export interface GoogleOAuthClientStatus {
  configured: boolean;
  source: 'stored' | 'env' | null;
  client_id_hint: string | null;
  redirect_uris: { calendar: string; email: string };
  origin: string;
  console_url: string;
  steps: GoogleOAuthStep[];
  warnings?: string[];
}

export const getGoogleOAuthClient = () => getJson<GoogleOAuthClientStatus>('/api/google/oauth-client');

export function saveGoogleOAuthClient(body: { client_id: string; client_secret: string } | { client_secret_json: string }) {
  return json<GoogleOAuthClientStatus>('/api/google/oauth-client', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body) }, 'Google OAuth client');
}

export function deleteGoogleOAuthClient() {
  return json<{ ok: boolean }>('/api/google/oauth-client', { method: 'DELETE' }, 'Google OAuth client');
}

export function checkGoogleOAuthClient() {
  return json<{ ok: boolean; detail: string }>('/api/google/oauth-client/check', { method: 'POST' }, 'Google OAuth client check');
}
