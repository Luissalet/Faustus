import { ApiError, asArray, getJson, responseReason } from './api';

/**
 * UX-10: the prompt library (routes/prompts_routes.py).
 *
 * `renderPrompt` only ever returns text — it calls no model and sends
 * nothing. Composer.tsx (or whichever screen embeds the picker) pastes the
 * result into the draft; sending it is the same click as sending anything
 * else typed by hand.
 */

export interface PromptVariable {
  name: string;
  type: 'string' | 'number' | 'boolean' | 'choice';
  required: boolean;
  default?: unknown;
  choices?: string[];
  help?: string;
}

export interface PromptExample {
  label: string;
  values: Record<string, unknown>;
}

export interface PromptTemplate {
  id: string;
  name: string;
  description: string;
  body: string;
  variables: PromptVariable[];
  scope: 'personal' | 'project';
  projectId: string | null;
  tags: string[];
  examples: PromptExample[];
  version: number;
  historyCount: number;
  favorite: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface PromptDraft {
  name: string;
  description?: string;
  body: string;
  variables?: PromptVariable[];
  scope?: 'personal' | 'project';
  projectId?: string | null;
  tags?: string[];
  examples?: PromptExample[];
}

function fromServer(raw: Record<string, unknown>): PromptTemplate {
  return {
    id: String(raw.id ?? ''),
    name: String(raw.name ?? ''),
    description: String(raw.description ?? ''),
    body: String(raw.body ?? ''),
    variables: asArray<PromptVariable>(raw.variables),
    scope: raw.scope === 'project' ? 'project' : 'personal',
    projectId: raw.project_id ? String(raw.project_id) : null,
    tags: asArray<string>(raw.tags),
    examples: asArray<PromptExample>(raw.examples),
    version: typeof raw.version === 'number' ? raw.version : 1,
    historyCount: typeof raw.history_count === 'number' ? raw.history_count : 0,
    favorite: Boolean(raw.favorite),
    createdAt: String(raw.created_at ?? ''),
    updatedAt: String(raw.updated_at ?? ''),
  };
}

function toServer(draft: PromptDraft): Record<string, unknown> {
  return {
    name: draft.name,
    description: draft.description ?? '',
    body: draft.body,
    variables: draft.variables ?? [],
    scope: draft.scope ?? 'personal',
    project_id: draft.projectId ?? null,
    tags: draft.tags ?? [],
    examples: draft.examples ?? [],
  };
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

export async function listPrompts(opts: { q?: string; projectId?: string; scope?: 'personal' | 'project' } = {}, signal?: AbortSignal): Promise<PromptTemplate[]> {
  const params = new URLSearchParams();
  if (opts.q) params.set('q', opts.q);
  if (opts.projectId) params.set('project_id', opts.projectId);
  if (opts.scope) params.set('scope', opts.scope);
  const qs = params.toString();
  const data = await getJson<{ templates?: Record<string, unknown>[] }>(`/api/prompts${qs ? `?${qs}` : ''}`, signal);
  return asArray<Record<string, unknown>>(data.templates).map(fromServer);
}

export async function createPrompt(draft: PromptDraft): Promise<PromptTemplate> {
  return fromServer(await send<Record<string, unknown>>('/api/prompts', 'POST', toServer(draft)));
}

export async function updatePrompt(id: string, draft: PromptDraft): Promise<PromptTemplate> {
  return fromServer(await send<Record<string, unknown>>(`/api/prompts/${encodeURIComponent(id)}`, 'PUT', toServer(draft)));
}

export async function deletePrompt(id: string): Promise<void> {
  await send(`/api/prompts/${encodeURIComponent(id)}`, 'DELETE');
}

export async function toggleFavoritePrompt(id: string): Promise<boolean> {
  return (await send<{ favorite: boolean }>(`/api/prompts/${encodeURIComponent(id)}/favorite`, 'POST')).favorite;
}

export async function renderPrompt(id: string, values: Record<string, unknown>): Promise<string> {
  return (await send<{ text: string }>(`/api/prompts/${encodeURIComponent(id)}/render`, 'POST', { values })).text;
}

export async function exportPrompts(ids: string[]): Promise<Record<string, unknown>[]> {
  const data = await getJson<{ templates?: Record<string, unknown>[] }>(`/api/prompts/export/batch?ids=${ids.map(encodeURIComponent).join(',')}`);
  return asArray(data.templates);
}

export interface ImportResult {
  imported: string[];
  needsResolution: { name: string; source_project_workspace?: string; error?: string }[];
}

export async function importPrompts(templates: Record<string, unknown>[], resolve: Record<string, string> = {}): Promise<ImportResult> {
  const data = await send<{ imported?: string[]; needs_resolution?: ImportResult['needsResolution'] }>('/api/prompts/import', 'POST', { templates, resolve });
  return { imported: data.imported ?? [], needsResolution: data.needs_resolution ?? [] };
}

/** Fills a template's variables from a value map, leaving unresolved tokens
 *  untouched — the client-side twin of the server's `render`, used for a
 *  live preview before the round trip that validates required variables. */
export function previewRender(body: string, values: Record<string, unknown>): string {
  return body.replace(/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g, (whole, name: string) =>
    name in values ? String(values[name]) : whole);
}
