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
  /** Live and in memory only on the bridge: who is typing / online in this chat. */
  presence?: 'available' | 'unavailable' | 'composing' | 'recording' | 'paused' | null;
  muted?: boolean;
  pinned?: boolean;
  archived?: boolean;
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
  /** Delivery/read receipt, own messages only. */
  status?: 'pending' | 'sent' | 'delivered' | 'read' | 'played';
  reactions?: { emoji: string; from: string; from_name: string; from_me: boolean }[];
  /** The quoted message, when this bubble is a reply. */
  reply_to?: { id: string; from_name: string; from_me?: boolean; text: string };
  edited?: boolean;
  /** Revoked ("this message was deleted"): the row stays, render it as such. */
  deleted?: boolean;
  forwarded?: boolean;
  /** jids mentioned in a group message. */
  mentions?: string[];
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
export interface WaSendOpts {
  /** Reply-quote: the id of the message this one replies to. */
  quote?: string;
  /** jids mentioned in the text (group chats). */
  mentions?: string[];
}
export const waSend = (to: string, text: string, opts?: WaSendOpts) =>
  post<WaSendResult>('/api/whatsapp/send', { to, text, quote: opts?.quote, mentions: opts?.mentions }, 'whatsapp/send');

/** Ask the phone for older messages of one chat; they arrive a few seconds later — poll `waMessages` again. */
export const waHistory = (chat: string, count = 50) => post<{ ok: boolean; before_ts: number }>('/api/whatsapp/history', { chat, count }, 'whatsapp/history');

/** Voice note → text through Faustus's speech provider (cached per message). */
export const waTranscribe = (id: string) => post<{ id: string; text: string; cached: boolean }>('/api/whatsapp/transcribe', { id }, 'whatsapp/transcribe');

export const waMarkRead = (chat?: string) => post<{ ok: boolean }>('/api/whatsapp/mark-read', chat ? { chat } : {}, 'whatsapp/mark-read');

/* ── WhatsApp-Web-like actions: react, delete, forward, edit, typing, presence, search, upload, assist ── */

/** Add (or, with `emoji: ''`, remove) a reaction on one message. */
export const waReact = (id: string, emoji: string) => post<{ ok: boolean }>('/api/whatsapp/react', { id, emoji }, 'whatsapp/react');

/** Revoke ("delete for everyone") one of your own messages. */
export const waDelete = (id: string) => post<{ ok: boolean }>('/api/whatsapp/delete', { id }, 'whatsapp/delete');

/** Forward one message to another chat. */
export const waForward = (id: string, to: string) => post<{ ok: boolean; id: string }>('/api/whatsapp/forward', { id, to }, 'whatsapp/forward');

/** Edit an own text message (WhatsApp allows this for ~15 minutes). */
export const waEdit = (id: string, text: string) => post<{ ok: boolean }>('/api/whatsapp/edit', { id, text }, 'whatsapp/edit');

/** Tell the other side you are composing/recording/paused in this chat. */
export const waTyping = (chat: string, state: 'composing' | 'recording' | 'paused') =>
  post<{ ok: boolean }>('/api/whatsapp/typing', { chat, state }, 'whatsapp/typing');

/** Subscribe to live presence for one chat (call when it is opened). */
export const waSubscribe = (chat: string) => post<{ ok: boolean }>('/api/whatsapp/subscribe', { chat }, 'whatsapp/subscribe');

/** Substring search over message text, newest first; `chat` narrows to one chat. */
export async function waSearch(q: string, chat?: string, limit = 50): Promise<WaMessage[]> {
  const params = new URLSearchParams({ q });
  if (chat) params.set('chat', chat);
  params.set('limit', String(limit));
  const d = await getJson<{ messages: WaMessage[] }>(`/api/whatsapp/search?${params.toString()}`);
  return d.messages ?? [];
}

export interface WaUploadOpts {
  filename?: string;
  caption?: string;
  quote?: string;
  /** true → sent as a push-to-talk voice note rather than a plain audio file. */
  voice?: boolean;
}

/** Send a photo, a document or a recorded voice note to one chat. */
export async function waUpload(to: string, file: Blob, opts: WaUploadOpts = {}): Promise<WaSendResult> {
  const form = new FormData();
  form.append('to', to);
  form.append('file', file, opts.filename || (file instanceof File ? file.name : 'file'));
  if (opts.caption) form.append('caption', opts.caption);
  if (opts.quote) form.append('quote', opts.quote);
  if (opts.voice) form.append('voice', '1');
  const r = await ok(await fetch('/api/whatsapp/upload', { method: 'POST', credentials: 'same-origin', body: form }), 'whatsapp/upload');
  return (await r.json()) as WaSendResult;
}

export type WaAssistTask = 'summarize' | 'draft_reply' | 'translate' | 'custom';

export interface WaAssistResult {
  text: string;
  messages: number;
  task?: WaAssistTask;
  note?: string;
}

/** Ask Faustus about one chat (summary, a draft in the owner's voice, a translation…): text back, nothing is ever sent. */
export const waAssist = (chat: string, task: WaAssistTask, instruction?: string, hours?: number) =>
  post<WaAssistResult>('/api/whatsapp/assist', { chat, task, instruction: instruction || '', hours: hours ?? 48 }, 'whatsapp/assist');
