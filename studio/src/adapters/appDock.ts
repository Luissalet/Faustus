import { useEffect, useRef, useSyncExternalStore } from 'react';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';
import { t } from '../i18n';

/**
 * App Dock (`/api/app-dock`, contract: Hoard Hub docs/APP_DOCK.md): a Hoard's full interface in a side-panel
 * tab, the person using it by hand while the agent sees the page and acts on it. This file is the host's
 * plumbing: the REST calls, which app each chat has docked (kept per chat in the browser), the stream of
 * server events and the registry that lets an event reach the frame the App tab mounted.
 */

export interface DockApp {
  id: string; name: string; url: string; state: string; icon: boolean; purpose: string; launchable: boolean; bridge: boolean;
}
export interface DockTool { name: string; description?: string; inputSchema?: unknown }
export interface DockServerState {
  app_id: string | null; name: string | null; url: string | null; tools: DockTool[];
  context: Record<string, unknown>; ready: boolean; updated: number | null; host?: boolean;
}
export interface DockSel { appId: string; name: string; url: string }
export interface DockActor { id: string; name: string; color: string }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION,
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  let body: unknown = null;
  try { body = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    const o = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    throw new ApiError(String(o.error || o.detail || `${path} ${t('responded')} ${response.status}`), response.status);
  }
  return body as T;
}

const post = <T,>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) });

export const listApps = () => request<{ apps: DockApp[]; hub?: string; available: boolean; error?: string }>('/api/app-dock/apps');
export const iconUrl = (id: string) => `/api/app-dock/icon/${encodeURIComponent(id)}`;
/** Dock an app in the chat. The server starts it through the Hub when it is down, so this can take a while. */
export const openApp = (session: string, app: string) =>
  post<DockServerState & { started?: boolean }>('/api/app-dock/open', { session, app });
export const closeApp = (session: string) => post<{ closed: boolean }>('/api/app-dock/close', { session });
export const getDockState = (session: string) => request<DockServerState>('/api/app-dock/state?session=' + encodeURIComponent(session));
export const postDockState = (body: { session: string; ready?: boolean; tools?: unknown; context?: unknown; app?: unknown; viewport?: { w: number; h: number } }) =>
  post<DockServerState>('/api/app-dock/state', body).catch(() => null);
export const postDockResult = (body: { session: string; id: string; ok: boolean; result?: unknown; error?: unknown }) =>
  post<{ resolved: boolean }>('/api/app-dock/result', body).catch(() => null);

/* ── which app each chat has docked ── */

const SEL_PREFIX = 'faustus.appdock.';
const sels = new Map<string, DockSel | null>();
const selListeners = new Set<() => void>();

export function readSel(session: string): DockSel | null {
  if (sels.has(session)) return sels.get(session) ?? null;
  let value: DockSel | null = null;
  try {
    const raw = JSON.parse(localStorage.getItem(SEL_PREFIX + session) || 'null');
    if (raw && typeof raw.appId === 'string' && typeof raw.url === 'string') {
      value = { appId: raw.appId, name: String(raw.name || raw.appId), url: raw.url };
    }
  } catch { /* storage unavailable: the choice lives in memory */ }
  sels.set(session, value);
  return value;
}

export function setSel(session: string, value: DockSel | null): void {
  const current = sels.get(session) ?? null;
  if (current && value && current.appId === value.appId && current.url === value.url && current.name === value.name) return;
  sels.set(session, value);
  try {
    if (value) localStorage.setItem(SEL_PREFIX + session, JSON.stringify(value));
    else localStorage.removeItem(SEL_PREFIX + session);
  } catch { /* private mode */ }
  selListeners.forEach(fn => fn());
}

function subscribeSel(fn: () => void): () => void {
  selListeners.add(fn);
  return () => { selListeners.delete(fn); };
}

export function useDockSel(session: string | null): DockSel | null {
  return useSyncExternalStore(subscribeSel, () => (session ? readSel(session) : null), () => null);
}

/* ── the frame the App tab mounted, per chat ── */

export interface FrameHandle {
  post: (message: Record<string, unknown>) => void;
  isReady: () => boolean;
  whenReady: (ms: number) => Promise<boolean>;
}
const frames = new Map<string, FrameHandle>();

export function registerFrame(session: string, handle: FrameHandle): () => void {
  frames.set(session, handle);
  return () => { if (frames.get(session) === handle) frames.delete(session); };
}

async function waitForFrame(session: string, ms: number): Promise<FrameHandle | null> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const handle = frames.get(session);
    if (handle) return handle;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return frames.get(session) ?? null;
}

/* ── the server's events ── */

interface CallEvent { id: string; tool: string; args?: Record<string, unknown>; actor?: DockActor }

async function relayCall(session: string, call: CallEvent, openPanel: () => void): Promise<void> {
  const fail = (code: string, message: string) =>
    postDockResult({ session, id: call.id, ok: false, error: { code, message } });
  let handle = frames.get(session) ?? null;
  if (!handle) {
    openPanel(); // the tab is closed: open it, the frame mounts and says hello
    handle = await waitForFrame(session, 6000);
  }
  if (!handle) { await fail('no_host', 'The App tab could not be shown for this conversation.'); return; }
  if (!handle.isReady() && !(await handle.whenReady(5000))) {
    await fail('not_ready', 'The app page did not answer the handshake (it may have no agent bridge yet).');
    return;
  }
  handle.post({ type: 'dock/call', id: call.id, tool: call.tool, args: call.args ?? {}, actor: call.actor });
}

/**
 * Keeps the chat's event stream open for as long as the chat is on screen: an `open` from the agent opens the
 * App tab, `call`/`changed`/`cursor` are relayed to the framed page.
 */
export function useAppDockEvents(session: string | null, openPanel: () => void): void {
  const openRef = useRef(openPanel);
  openRef.current = openPanel;
  useEffect(() => {
    if (!session || typeof EventSource === 'undefined') return;
    const sid = session;
    const source = new EventSource('/api/app-dock/events?session=' + encodeURIComponent(sid));
    let quiet = false; // the next `open` is our own re-registration after a reload, not the agent's request
    const parse = (event: Event): Record<string, any> => {
      try { return JSON.parse((event as MessageEvent).data) ?? {}; } catch { return {}; }
    };
    source.addEventListener('hello', event => {
      const docked = (parse(event).docked ?? {}) as Partial<DockServerState>;
      if (docked.app_id) {
        setSel(sid, { appId: docked.app_id, name: docked.name || docked.app_id, url: docked.url || readSel(sid)?.url || '' });
      } else {
        const local = readSel(sid);
        if (local) { quiet = true; void openApp(sid, local.appId).catch(() => { quiet = false; }); }
      }
    });
    source.addEventListener('open', event => {
      // The browser also fires a bare open when the stream connects; only the server's message is a request.
      if (!(event instanceof MessageEvent)) return;
      const d = parse(event);
      if (d.app) setSel(sid, { appId: String(d.app), name: String(d.name || d.app), url: String(d.url || '') });
      if (quiet) { quiet = false; return; }
      openRef.current();
    });
    source.addEventListener('close', () => setSel(sid, null));
    source.addEventListener('call', event => { void relayCall(sid, parse(event) as CallEvent, () => openRef.current()); });
    source.addEventListener('changed', event => {
      const d = parse(event);
      frames.get(sid)?.post({ type: 'dock/changed', server: d.server, tool: d.tool });
    });
    source.addEventListener('cursor', event => {
      const d = parse(event);
      frames.get(sid)?.post({ type: 'dock/cursor', actor: d.actor, state: d.state });
    });
    return () => source.close();
  }, [session]);
}
