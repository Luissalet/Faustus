import { useEffect, useRef } from 'react';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';
import type { Doc, Patch } from '../lib/pizarraModel';
import type { Style } from '../lib/hojaModel';
import { t } from '../i18n';

/**
 * Pizarra and Hoja (`/api/pizarra`, `/api/hoja`, `/api/surfaces/events`): the REST calls, and the one stream of
 * server events per chat (an edit by the agent, the agent's cursor, a surface opened by a tool) that the open
 * surfaces subscribe to. The files and the rules are the server's (src/pizarra.py, src/hoja.py).
 */

export interface SurfaceMeta { id: string; title: string; session_id?: string; project_id?: string; rev: number; updated: number; created: number; nodes?: number; edges?: number; tabs?: number; cells?: number }
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

/* -- Hoja -- */
export interface HojaChartData { id: string; type: 'line' | 'bar' | 'pie' | 'scatter'; title: string; at: string | null; w: number; h: number; categories: string[]; series: { name: string | null; values: (number | null)[] }[] }
export interface HojaFilter { range: string; rg: number[]; col: number; values?: string[] | null; where?: string | null }
export interface HojaTab {
  id: string; name: string; used: [number, number]; cols: Record<string, number>; rows: Record<string, number>;
  freeze: { rows: number; cols: number }; filter: HojaFilter | null; hidden: number[]; cf: Record<string, unknown>[]; charts: HojaChartData[];
}
export type HojaCellRow = [number, number, unknown, string, string | null, number, Record<string, unknown> | null];
export interface HojaWindow { rev: number; tab: string; cells: HojaCellRow[]; styles: Record<string, Style> }
export interface HojaChanges { tabs: Record<string, { cells?: [number, number][]; full?: boolean }>; struct: boolean }
export interface HojaEditResult extends HojaChanges { rev: number; results: string[]; error?: { code: string; message: string } }
const sid = (id: string) => encodeURIComponent(id);
export const listSheets = (session: string) => request<{ sheets: SurfaceMeta[] }>('/api/hoja?session=' + encodeURIComponent(session));
export const createSheet = (session: string, title = '', project = '', csv?: string) => post<SurfaceMeta>('/api/hoja', { title, session, project, ...(csv ? { csv } : {}) });
export const getSheet = (id: string) => request<{ meta: SurfaceMeta; tabs: HojaTab[] }>('/api/hoja/' + sid(id));
export const sheetWindow = (id: string, tab: string, r0: number, c0: number, r1: number, c1: number) =>
  request<HojaWindow>(`/api/hoja/${sid(id)}/window?tab=${encodeURIComponent(tab)}&r0=${r0}&c0=${c0}&r1=${r1}&c1=${c1}`);
export const sheetTab = (id: string, tab: string) => request<HojaTab & { rev: number }>(`/api/hoja/${sid(id)}/tab/${encodeURIComponent(tab)}`);
export const editSheet = (id: string, ops: unknown[], tab: string) => post<HojaEditResult>(`/api/hoja/${sid(id)}/edit`, { ops, client: CLIENT_ID, view: { tab } });
export const undoSheet = (id: string) => post<{ ok: boolean; rev: number } & HojaChanges>(`/api/hoja/${sid(id)}/undo`, { client: CLIENT_ID });
export const redoSheet = (id: string) => post<{ ok: boolean; rev: number } & HojaChanges>(`/api/hoja/${sid(id)}/redo`, { client: CLIENT_ID });
export const renameSheet = (id: string, title: string) => post<SurfaceMeta>(`/api/hoja/${sid(id)}/rename`, { title });
export const deleteSheet = (id: string) => request<{ deleted: boolean }>('/api/hoja/' + sid(id), { method: 'DELETE' });
export const sheetState = (id: string, body: { session: string; tab?: string; sel?: { r0: number; c0: number; r1: number; c1: number } | null; open?: boolean }) =>
  post<{ open: boolean }>(`/api/hoja/${sid(id)}/state`, body).catch(() => null);
export const openSheetFile = (workspace: string, path: string, session: string, project = '') => post<SurfaceMeta>('/api/hoja/open-file', { workspace, path, session, project });
export const sheetXlsxUrl = (id: string) => `/api/hoja/${sid(id)}/export.xlsx`;
export const sheetCsvUrl = (id: string, tab: string) => `/api/hoja/${sid(id)}/export.csv?tab=${encodeURIComponent(tab)}`;
export async function importSheet(id: string, file: File, tab: string): Promise<{ rev: number }> {
  const format = /\.csv$/i.test(file.name) ? 'csv' : 'xlsx';
  const r = await fetch(`/api/hoja/${sid(id)}/import?format=${format}&tab=${encodeURIComponent(tab)}`, {
    method: 'POST', body: await file.arrayBuffer(), credentials: 'same-origin', headers: { [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION, 'Content-Type': 'application/octet-stream' },
  });
  if (!r.ok) { let msg = ''; try { msg = String(((await r.json()) as { error?: string }).error || ''); } catch { /* not JSON */ } throw new ApiError(msg || t('Could not read that file.'), r.status); }
  return (await r.json()) as { rev: number };
}

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
