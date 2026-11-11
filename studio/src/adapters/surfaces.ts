import { useEffect, useRef } from 'react';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';
import type { Doc, Patch } from '../lib/pizarraModel';
import { t } from '../i18n';

/**
 * Pizarra and Hoja (`/api/pizarra`, `/api/hoja`, `/api/surfaces/events`): the REST calls, and the one stream of
 * server events per chat (an edit by the agent, the agent's cursor, a surface opened by a tool) that the open
 * surfaces subscribe to. The files and the rules are the server's (src/pizarra.py, src/hoja.py).
 */

export interface SurfaceMeta { id: string; title: string; session_id?: string; project_id?: string; rev: number; updated: number; created: number; nodes?: number; edges?: number; tabs?: number }
export interface SurfaceActor { id: string; name: string; color: string }
export interface SurfaceEvent {
  event: 'open' | 'patch' | 'cells' | 'cursor' | 'meta' | 'hello';
  data: Record<string, any>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION,
      ...(init?.body && typeof init.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers ?? {}),
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
export const post = <T,>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) });

/* ── Pizarra ── */
export const listBoards = (session: string) => request<{ boards: SurfaceMeta[] }>('/api/pizarra?session=' + encodeURIComponent(session));
export const createBoard = (session: string, title = '', project = '', doc?: unknown) => post<SurfaceMeta>('/api/pizarra', { title, session, project, ...(doc ? { doc } : {}) });
export const getBoard = (id: string) => request<{ meta: SurfaceMeta; doc: Doc }>('/api/pizarra/' + encodeURIComponent(id));
export const patchBoard = (id: string, client: string, patch: Patch) => post<{ rev: number }>(`/api/pizarra/${encodeURIComponent(id)}/patch`, { client, patch });
export const renameBoard = (id: string, title: string) => post<SurfaceMeta>(`/api/pizarra/${encodeURIComponent(id)}/rename`, { title });
export const deleteBoard = (id: string) => request<{ deleted: boolean }>('/api/pizarra/' + encodeURIComponent(id), { method: 'DELETE' });
export const boardState = (id: string, body: { session: string; selection?: string[]; viewport?: { x: number; y: number; zoom: number; w: number; h: number }; open?: boolean }) =>
  post<{ open: boolean }>(`/api/pizarra/${encodeURIComponent(id)}/state`, body).catch(() => null);
export const boardSvgUrl = (id: string, dark: boolean) => `/api/pizarra/${encodeURIComponent(id)}/export.svg?dark=${dark ? 1 : 0}`;
export const boardCanvasUrl = (id: string) => `/api/pizarra/${encodeURIComponent(id)}/export.canvas`;
export const boardAssetUrl = (id: string, file: string) => `/api/pizarra/${encodeURIComponent(id)}/${file.replace(/^assets\//, 'assets/')}`;
export const importCanvas = (id: string, text: string) => request<{ rev: number; nodes: number; edges: number }>(`/api/pizarra/${encodeURIComponent(id)}/import`, { method: 'POST', body: text, headers: { 'Content-Type': 'application/json' } });
export async function uploadBoardAsset(id: string, blob: Blob): Promise<string> {
  const ext = (blob.type.split('/')[1] || 'png').replace('jpeg', 'jpg').replace('svg+xml', 'svg');
  const r = await fetch(`/api/pizarra/${encodeURIComponent(id)}/asset?ext=${ext}`, {
    method: 'POST', body: blob, credentials: 'same-origin', headers: { [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION, 'Content-Type': 'application/octet-stream' },
  });
  if (!r.ok) throw new ApiError(t('Could not upload the image.'), r.status);
  return ((await r.json()) as { file: string }).file;
}

/* ── the stream ── */

type Listener = (e: SurfaceEvent) => void;
const listeners = new Set<Listener>();
export function onSurfaceEvent(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** One client id per window, so a window ignores the edits it sent itself. */
export const CLIENT_ID = Math.random().toString(36).slice(2, 10);

/**
 * Keeps the chat's surface stream open while the chat is on screen. `onOpen` is called when the agent creates
 * or opens a whiteboard / sheet; edits and cursors go to whoever subscribed with `onSurfaceEvent`.
 */
export function useSurfaceEvents(session: string | null, onOpen: (kind: 'pizarra' | 'hoja', id: string, title: string, focus: boolean) => void): void {
  const openRef = useRef(onOpen);
  openRef.current = onOpen;
  useEffect(() => {
    if (!session || typeof EventSource === 'undefined') return;
    const source = new EventSource('/api/surfaces/events?session=' + encodeURIComponent(session));
    const names: SurfaceEvent['event'][] = ['open', 'patch', 'cells', 'cursor', 'meta', 'hello'];
    for (const name of names) {
      source.addEventListener(name, (event) => {
        let data: Record<string, any> = {};
        try { data = JSON.parse((event as MessageEvent).data) ?? {}; } catch { /* ignore */ }
        if (name === 'open' && (data.kind === 'pizarra' || data.kind === 'hoja')) openRef.current(data.kind, String(data.id), String(data.title || ''), data.focus !== false);
        listeners.forEach((fn) => fn({ event: name, data }));
      });
    }
    return () => source.close();
  }, [session]);
}
