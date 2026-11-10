import { t } from '../i18n';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER } from './api';

/** Obsidian Bases (`/api/brain/bases*`, src/brain/bases.py): `.base` views over the Cerebro's notes. */
export interface BaseFile { path: string; name: string; views: string[]; ok: boolean; errors: string[]; updated?: string }
export interface BaseColumn { id: string; label: string }
export interface BaseRow { path: string; title: string; cells: Record<string, string> }
export interface BaseView {
  name: string; type: 'table' | 'cards' | 'list' | 'map'; columns: BaseColumn[]; rows: BaseRow[];
  groups: { key: string; label: string; rows: BaseRow[] }[] | null; summaries: Record<string, { name: string; value: string }>;
  total: number; shown: number; truncated: boolean; note?: string;
}
export interface BaseResult { path: string | null; notes_scanned: number; matched: number; views: BaseView[]; errors: string[]; warnings: string[] }
export interface BaseSpec {
  tags?: string[]; folder?: string; where?: string[]; columns?: string[]; view_type?: string; view_name?: string;
  group_by?: string; sort_by?: string; sort_direction?: string; limit?: number;
}

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

const BASE = '/api/brain/bases';
export async function listBases(): Promise<BaseFile[]> {
  const r = await request<{ bases?: BaseFile[] }>(BASE);
  return Array.isArray(r.bases) ? r.bases : [];
}
export const queryBase = (path: string, view?: string) => request<BaseResult>(`${BASE}/query`, { method: 'POST', body: JSON.stringify({ path, view }) });
export const readBase = (path: string) => request<{ path: string; yaml: string; validation: { ok: boolean; errors: string[]; warnings: string[] } }>(`${BASE}/file?path=${encodeURIComponent(path)}`);
export const saveBase = (path: string, body: { yaml?: string; spec?: BaseSpec }) =>
  request<{ path: string; created: boolean }>(`${BASE}/file`, { method: 'PUT', body: JSON.stringify({ path, ...body }) });
export const deleteBase = (path: string) => request<{ deleted: string }>(`${BASE}/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
export const validateBase = (yaml: string) => request<{ ok: boolean; errors: string[]; warnings: string[] }>(`${BASE}/validate`, { method: 'POST', body: JSON.stringify({ yaml }) });
