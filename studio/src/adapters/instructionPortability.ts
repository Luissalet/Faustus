import { ApiError, getJson, responseReason } from './api';

export interface Source { id: string; name: string; description: string }
export interface Catalog { skills: Source[]; rules: Source[]; agents: Source[]; warnings: string[] }
export interface Bundle { target: string; files: { path: string; content: string; encoding?: string }[]; warnings: string[] }
export interface ExportOptions { target: string; workspace: string; destination: string; skills?: string[]; rules?: string[]; agents?: string[]; overwrite?: boolean }
export interface Receipt { created: number; updated: number; unchanged: number; conflicts: string[]; warnings: string[] }
export interface MarkdownHook { name: string; event: string; message: string; pattern?: string; conditions?: { field: string; operator: string; pattern: string }[]; enabled?: boolean; markdown?: string; evidence?: string; path?: string }

const base = '/api/instruction-portability';
async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(base + path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!response.ok) throw new ApiError(await responseReason(response, base + path), response.status);
  return response.json() as Promise<T>;
}
export const catalog = (workspace: string) => getJson<Catalog>(base + '/catalog?workspace=' + encodeURIComponent(workspace));
export const previewBundle = (options: ExportOptions) => post<Bundle>('/preview', options);
export const exportBundle = (options: ExportOptions) => post<Receipt>('/export', options);
export const loadHooks = (workspace: string) => getJson<{ rules: MarkdownHook[]; warnings: { message: string }[] }>(base + '/hooks?workspace=' + encodeURIComponent(workspace));
export const saveHook = (workspace: string, rule: MarkdownHook, overwrite = false) => post<{ status: string; path: string; warnings?: string[] }>('/hooks/save', { workspace, rule, overwrite });
export const proposeHooks = (messages: { role: string; content: string }[], session_id = '') => post<{ proposals: MarkdownHook[]; notes: { message: string }[] }>('/hooks/propose', { messages, session_id });
