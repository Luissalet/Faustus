import { ApiError, getJson } from './api';

/**
 * Lifecycle hooks (`src/lifecycle_hooks.py`, `routes/lifecycle_hooks_routes.py`)
 * — user-configurable automation on points of the agent's turn: run a
 * command and feed its output back, inject a fixed note, or warn. Admin-only,
 * same gate as the tool argument rules this card sits next to in Settings →
 * Tools.
 */

export type HookEvent = 'session_start' | 'turn_start' | 'pre_tool' | 'post_tool' | 'turn_end' | 'pre_compact';
export type HookAction = 'command' | 'inject' | 'warn';
export type HookScope = 'global' | 'project';

export interface HookMatch {
  tool?: string;
  path?: string;
  command?: string;
  text?: string;
}

export interface Hook {
  id: string;
  enabled: boolean;
  event: HookEvent;
  name: string;
  match: HookMatch;
  action: HookAction;
  command: string | null;
  text: string | null;
  timeout_s: number | null;
  max_output_chars: number;
  scope: HookScope;
  note: string;
}

export interface HookPreset {
  preset_id: string;
  description: string;
}

export interface LifecycleHooksData {
  hooks: Hook[];
  presets: HookPreset[];
  events: HookEvent[];
  actions: HookAction[];
}

export interface HookRunResult {
  hook_id: string;
  name: string;
  event: string;
  action: string;
  ok: boolean;
  output: string;
  error: string;
  duration_ms: number;
  timed_out: boolean;
  level: 'info' | 'warn';
  exit_code: number | null;
}

export interface HookLogEntry extends HookRunResult {
  ts: number;
}

async function readError(response: Response, path: string): Promise<string> {
  try {
    const body = (await response.clone().json()) as { error?: unknown; detail?: unknown };
    if (typeof body?.error === 'string' && body.error.trim()) return body.error;
    if (typeof body?.detail === 'string' && body.detail.trim()) return body.detail;
  } catch {
    /* not JSON */
  }
  return `${path} responded ${response.status}`;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await readError(response, path), response.status);
  return (await response.json()) as T;
}

export async function loadLifecycleHooks(): Promise<LifecycleHooksData> {
  return getJson<LifecycleHooksData>('/api/lifecycle-hooks');
}

/** Replaces the whole list, same convention as `saveToolArgRules`. */
export async function saveLifecycleHooks(hooks: Hook[]): Promise<Hook[]> {
  const path = '/api/lifecycle-hooks';
  const response = await fetch(path, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ hooks }),
  });
  if (!response.ok) throw new ApiError(await readError(response, path), response.status);
  const data = (await response.json()) as { hooks: Hook[] };
  return data.hooks;
}

/** Idempotent by id: adding a preset already present is a no-op. */
export async function addLifecycleHooksPreset(presetId: string): Promise<Hook[]> {
  const data = await postJson<{ hooks: Hook[] }>(`/api/lifecycle-hooks/presets/${encodeURIComponent(presetId)}`, {});
  return data.hooks;
}

export interface HookTestOutcome {
  /** Set when `run` is false: the ids that would fire, nothing executed. */
  matched?: string[];
  /** Set when `run` is true: what actually ran. */
  results?: HookRunResult[];
}

export async function testLifecycleHooks(event: HookEvent, ctx: Record<string, unknown>, run: boolean): Promise<HookTestOutcome> {
  return postJson<HookTestOutcome>('/api/lifecycle-hooks/test', { event, ctx, run });
}

export async function lifecycleHooksLog(limit = 50): Promise<HookLogEntry[]> {
  const data = await getJson<{ results: HookLogEntry[] }>(`/api/lifecycle-hooks/log?limit=${encodeURIComponent(String(limit))}`);
  return data.results ?? [];
}

export function emptyHook(): Hook {
  return {
    id: '', enabled: true, event: 'post_tool', name: '', match: {}, action: 'command',
    command: '', text: null, timeout_s: null, max_output_chars: 4000, scope: 'global', note: '',
  };
}

/** One line summarising `match` for the hook table, e.g. "tool: edit_file|write_file · path: *.py". */
export function matchSummary(match: HookMatch): string {
  const parts: string[] = [];
  if (match.tool) parts.push(`tool: ${match.tool}`);
  if (match.path) parts.push(`path: ${match.path}`);
  if (match.command) parts.push(`command: ${match.command}`);
  if (match.text) parts.push(`text: ${match.text}`);
  return parts.join(' · ');
}
