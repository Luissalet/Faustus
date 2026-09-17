import { ApiError, getJson } from './api';

/**
 * WhatsApp — Studio side of `routes/whatsapp_routes.py` + `src/whatsapp_bridge.py`.
 *
 * A local Node bridge links the person's own WhatsApp account (linked
 * device, like WhatsApp Web) and mirrors chats/messages into Faustus:
 * reading what people wrote, answering from a chat, a daily digest card.
 * Nothing here re-implements the bridge — it only calls the routes it
 * exposes, the same shape `adapters/processCenter.ts` and
 * `adapters/connectors.ts` already use: `credentials: 'same-origin'`, and a
 * failed response throws `ApiError` with the server's own `detail` text.
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function ok(response: Response, what: string): Promise<Response> {
  if (response.ok) return response;
  let detail = '';
  try {
    const body = (await response.json()) as { detail?: unknown; error?: unknown };
    if (typeof body.detail === 'string') detail = body.detail;
    else if (typeof body.error === 'string') detail = body.error;
    else if (body.detail) detail = JSON.stringify(body.detail);
  } catch {
    /* not JSON */
  }
  throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
}

async function json<T>(path: string, init: RequestInit, what: string): Promise<T> {
  const r = await ok(await fetch(path, { credentials: 'same-origin', ...init }), what);
  const text = await r.text();
  return (text ? JSON.parse(text) : {}) as T;
}

const post = <T,>(path: string, body: unknown, what: string) => json<T>(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }, what);

/* ── Status ── */

export type WaStatusKind = 'stopped' | 'starting' | 'qr' | 'connected' | 'disconnected' | 'logged_out';

export interface WaMe {
  jid: string;
  name: string;
}

export interface WaCounts {
  contacts: number;
  chats: number;
  messages: number;
}

export interface WhatsAppStatus {
  status: WaStatusKind;
  qr?: string | null;
  me?: WaMe | null;
  lastError?: string | null;
  counts?: WaCounts | null;
  installed: boolean;
  node?: boolean;
  port: number;
}

export const waStatus = () => getJson<WhatsAppStatus>('/api/whatsapp/status');

/* ── Lifecycle ── */

export interface WaStartResult {
  started: boolean;
  already_running?: boolean;
  ready?: boolean;
  error?: string | null;
  status?: string;
}
export const waStart = () => post<WaStartResult>('/api/whatsapp/start', {}, 'whatsapp/start');

export interface WaStopResult {
  stopped: boolean;
  reason?: string | null;
}
export const waStop = () => post<WaStopResult>('/api/whatsapp/stop', {}, 'whatsapp/stop');

export const waLogout = () => post<{ ok: boolean }>('/api/whatsapp/logout', {}, 'whatsapp/logout');

/* ── Chats & messages ── */

export interface WaChat {
  jid: string;
  name: string;
  is_group: boolean;
  last_ts: number | null;
  last_text: string;
  unread: number;
}

export async function waChats(limit = 50): Promise<WaChat[]> {
  const d = await getJson<{ chats: WaChat[] }>(`/api/whatsapp/chats?limit=${encodeURIComponent(String(limit))}`);
  return d.chats ?? [];
}

export type WaMessageKind = 'text' | 'image' | 'video' | 'audio' | 'document' | 'sticker' | 'location' | 'contact' | 'reaction' | 'other';

export interface WaMessage {
  id: string;
  chat: string;
  chat_name: string;
  from: string;
  from_name: string;
  from_me: boolean;
  ts: number;
  text: string;
  kind: WaMessageKind;
  unread: boolean;
  /** Pulled attachment (voice note, photo, document): file name under the bridge's media store. */
  media?: string;
  mime?: string;
  /** Voice notes: duration in seconds and whether it was recorded as push-to-talk. */
  seconds?: number;
  voice?: boolean;
  /** Filled by the server once the voice note went through speech-to-text. */
  transcript?: string;
}

/** `<img src>` for a chat's profile picture (404 when the contact has none — hide the image). */
export const waAvatarUrl = (jid: string) => `/api/whatsapp/avatar?jid=${encodeURIComponent(jid)}`;

/** `<audio src>` / `<img src>` / download link for a pulled attachment. */
export const waMediaUrl = (name: string) => `/api/whatsapp/media/${encodeURIComponent(name)}`;

export interface WaMessagesInput {
  chat?: string;
  hours?: number;
  limit?: number;
  unread?: boolean;
}

export async function waMessages(input: WaMessagesInput = {}): Promise<WaMessage[]> {
  const params = new URLSearchParams();
  if (input.chat) params.set('chat', input.chat);
  params.set('hours', String(input.hours ?? 48));
  params.set('limit', String(input.limit ?? 200));
  params.set('unread', input.unread ? '1' : '0');
  const d = await getJson<{ messages: WaMessage[] }>(`/api/whatsapp/messages?${params.toString()}`);
  return d.messages ?? [];
}

/* ── Contacts ── */

export interface WaContact {
  jid: string;
  name: string;
  phone: string;
}

export async function waContacts(q = ''): Promise<WaContact[]> {
  const d = await getJson<{ contacts: WaContact[] }>(`/api/whatsapp/contacts?q=${encodeURIComponent(q)}`);
  return d.contacts ?? [];
}

/* ── Send & mark-read ── */

export interface WaSendResult {
  ok: boolean;
  to: string;
  jid: string;
  id: string;
}
export const waSend = (to: string, text: string) => post<WaSendResult>('/api/whatsapp/send', { to, text }, 'whatsapp/send');

/** Ask the phone for older messages of one chat; they arrive a few seconds later — poll `waMessages` again. */
export const waHistory = (chat: string, count = 50) => post<{ ok: boolean; before_ts: number }>('/api/whatsapp/history', { chat, count }, 'whatsapp/history');

/** Voice note → text through Faustus's speech provider (cached per message). */
export const waTranscribe = (id: string) => post<{ id: string; text: string; cached: boolean }>('/api/whatsapp/transcribe', { id }, 'whatsapp/transcribe');

export const waMarkRead = (chat?: string) => post<{ ok: boolean }>('/api/whatsapp/mark-read', chat ? { chat } : {}, 'whatsapp/mark-read');
