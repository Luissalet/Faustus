import { ApiError, getJson } from './api';

/**
 * The app's settings (`/api/auth/settings`) as Studio needs them: the
 * keybinds the person configured in the previous interface apply here too,
 * merged over the same defaults (static/js/keyboard-shortcuts.js).
 */

export const DEFAULT_KEYBINDS: Record<string, string> = {
  search: 'ctrl+k',
  toggle_sidebar: 'ctrl+b',
  new_session: 'ctrl+alt+n',
  fav_session: 'ctrl+alt+f',
  delete_session: 'ctrl+alt+d',
  cancel: 'escape',
  tts: 'alt+shift+t',
  incognito: 'ctrl+alt+i',
  settings: 'ctrl+,',
  focus_input: 'ctrl+/',
  open_calendar: 'ctrl+alt+c',
  open_compare: '',
  open_cookbook: '',
  open_research: '',
  open_gallery: '',
  open_library: '',
  open_memory: '',
  open_notes: '',
  open_tasks: '',
  open_theme: '',
};

let cached: Promise<Record<string, unknown>> | null = null;

export function getSettings(signal?: AbortSignal): Promise<Record<string, unknown>> {
  if (!cached) {
    cached = getJson<Record<string, unknown>>('/api/auth/settings', signal).catch((e) => {
      cached = null;
      throw e;
    });
  }
  return cached;
}

export function invalidateSettings(): void {
  cached = null;
}

export async function getKeybinds(): Promise<Record<string, string>> {
  try {
    const s = await getSettings();
    const raw = s.keybinds && typeof s.keybinds === 'object' ? (s.keybinds as Record<string, unknown>) : {};
    const out = { ...DEFAULT_KEYBINDS };
    for (const [k, v] of Object.entries(raw)) if (typeof v === 'string') out[k] = v;
    return out;
  } catch {
    return { ...DEFAULT_KEYBINDS };
  }
}

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

/**
 * AltGr arrives as Ctrl+Alt.
 *
 * On AZERTY, QWERTZ and most non-US layouts, AltGr is how you type
 * @ # { } [ ] | \ and €. The browser reports it as ctrlKey AND altKey, so
 * someone typing an ordinary character would silently fire a destructive
 * Ctrl+Alt shortcut — new chat, delete chat, incognito.
 *
 * `getModifierState('AltGraph')` is the real signal and is true for AltGr and
 * false for a genuine left Ctrl+Alt. It is not there in every browser, and on
 * macOS the Option key sets it too (where AltGr does not exist), so the
 * heuristic stays as the fallback: Ctrl+Alt plus a character that is not a
 * plain letter or digit is somebody typing, not somebody using a shortcut.
 */
function isAltGr(e: KeyboardEvent): boolean {
  if (IS_MAC) return false;
  if (!e.ctrlKey || !e.altKey || e.metaKey) return false;
  try {
    if (typeof e.getModifierState === 'function' && e.getModifierState('AltGraph')) return true;
  } catch {
    /* not every event implements it */
  }
  return e.key.length === 1 && !/^[a-z0-9]$/i.test(e.key);
}

export function matchesCombo(e: KeyboardEvent, combo: string): boolean {
  if (!combo) return false;
  if (isAltGr(e)) return false;
  const parts = combo.toLowerCase().split('+');
  const needCtrl = parts.includes('ctrl');
  const needAlt = parts.includes('alt');
  const needShift = parts.includes('shift');
  const key = parts.filter((p) => p !== 'ctrl' && p !== 'alt' && p !== 'shift')[0] ?? '';
  if (needCtrl !== (e.ctrlKey || e.metaKey)) return false;
  if (needAlt !== e.altKey) return false;
  if (needShift !== e.shiftKey) return false;
  return e.key.toLowerCase() === key;
}

/* ── Ajustes: reading and writing the app settings (admin) ─────────────── */

export type Settings = Record<string, unknown>;

async function okr(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

/** Fresh copy, bypassing the cache used by the shortcuts. */
export async function loadSettings(signal?: AbortSignal): Promise<Settings> {
  const s = await getJson<Settings>('/api/auth/settings', signal);
  cached = Promise.resolve(s);
  return s;
}

/** Posts only `patch`; the server merges and returns the whole set. */
export async function saveSettings(patch: Settings): Promise<Settings> {
  const r = await okr(
    await fetch('/api/auth/settings', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) }),
    'auth/settings',
  );
  const s = (await r.json()) as Settings;
  cached = Promise.resolve(s);
  return s;
}

export interface SchemaField {
  key: string;
  label: string;
  help: string;
  type: 'bool' | 'int' | 'float' | 'select' | 'list' | 'text' | string;
  min?: number;
  max?: number;
  step?: number;
  options?: { value: string; label: string }[];
  restart_hint?: boolean;
}

export interface SchemaGroup {
  key: string;
  title: string;
  help?: string;
  fields: SchemaField[];
}

/** The server's own description of every agent_* / browser_* / desktop_* key. */
export async function getAgentSchema(signal?: AbortSignal): Promise<{ groups: SchemaGroup[]; defaults: Settings }> {
  const data = await getJson<{ groups?: SchemaGroup[]; defaults?: Settings }>('/api/agent/settings/schema', signal);
  const groups = (data.groups ?? []).map((g) => ({
    ...g,
    fields: (g.fields ?? []).map((f) => ({
      ...f,
      options: Array.isArray(f.options) ? f.options.map((o) => (typeof o === 'string' ? { value: o, label: o } : (o as { value: string; label: string }))) : undefined,
    })),
  }));
  return { groups, defaults: data.defaults ?? {} };
}

/* ── Model endpoints ── */

export interface ModelEndpoint {
  id: string;
  name: string;
  baseUrl: string;
  hasKey: boolean;
  enabled: boolean;
  models: string[];
  online: boolean;
  status: string;
  pingError: string | null;
  kind: string;
  category: string;
  supportsTools: boolean | null;
  modelType: string;
}

function endpointFrom(raw: Record<string, unknown>): ModelEndpoint {
  return {
    id: String(raw.id ?? ''),
    name: typeof raw.name === 'string' ? raw.name : '',
    baseUrl: typeof raw.base_url === 'string' ? raw.base_url : '',
    hasKey: Boolean(raw.has_key),
    enabled: raw.is_enabled !== false,
    models: Array.isArray(raw.models) ? raw.models.map(String) : [],
    online: Boolean(raw.online),
    status: typeof raw.status === 'string' ? raw.status : '',
    pingError: typeof raw.ping_error === 'string' ? raw.ping_error : null,
    kind: typeof raw.endpoint_kind === 'string' ? raw.endpoint_kind : 'auto',
    category: typeof raw.category === 'string' ? raw.category : '',
    supportsTools: typeof raw.supports_tools === 'boolean' ? raw.supports_tools : null,
    modelType: typeof raw.model_type === 'string' ? raw.model_type : 'llm',
  };
}

export async function listEndpoints(signal?: AbortSignal): Promise<ModelEndpoint[]> {
  const data = await getJson<unknown>('/api/model-endpoints', signal);
  const list = Array.isArray(data) ? data : ((data as { endpoints?: unknown[] })?.endpoints ?? []);
  return (list as Record<string, unknown>[]).map(endpointFrom);
}

export async function addEndpoint(input: { name: string; baseUrl: string; apiKey: string; modelType: string; kind: string }): Promise<void> {
  const fd = new FormData();
  fd.append('name', input.name);
  fd.append('base_url', input.baseUrl);
  fd.append('api_key', input.apiKey);
  fd.append('model_type', input.modelType);
  fd.append('endpoint_kind', input.kind);
  await okr(await fetch('/api/model-endpoints', { method: 'POST', credentials: 'same-origin', body: fd }), 'model-endpoints');
}

export async function patchEndpoint(id: string, body: Record<string, unknown>): Promise<void> {
  await okr(
    await fetch(`/api/model-endpoints/${encodeURIComponent(id)}`, { method: 'PATCH', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    'model-endpoints/patch',
  );
}

/** No body: the server toggles `is_enabled` (the previous interface's behaviour). */
export async function toggleEndpoint(id: string): Promise<void> {
  await okr(await fetch(`/api/model-endpoints/${encodeURIComponent(id)}`, { method: 'PATCH', credentials: 'same-origin' }), 'model-endpoints/toggle');
}

export async function deleteEndpoint(id: string): Promise<void> {
  await okr(await fetch(`/api/model-endpoints/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' }), 'model-endpoints/delete');
}

/** Re-reads the model list from the endpoint itself (`?refresh=true`). */
export async function refreshEndpointModels(id: string): Promise<string[]> {
  const data = await getJson<{ models?: unknown[] }>(`/api/model-endpoints/${encodeURIComponent(id)}/models?refresh=true`);
  return Array.isArray(data.models) ? data.models.map((m) => (typeof m === 'string' ? m : String((m as { id?: unknown })?.id ?? ''))).filter(Boolean) : [];
}

export async function testEndpoint(baseUrl: string, apiKey: string): Promise<{ ok: boolean; models: string[]; error: string | null }> {
  const fd = new FormData();
  fd.append('base_url', baseUrl);
  fd.append('api_key', apiKey);
  const r = await fetch('/api/model-endpoints/test', { method: 'POST', credentials: 'same-origin', body: fd });
  const data = (await r.json().catch(() => ({}))) as Record<string, unknown>;
  const models = Array.isArray(data.models) ? data.models.map((m) => (typeof m === 'string' ? m : String((m as { id?: unknown })?.id ?? ''))).filter(Boolean) : [];
  return { ok: r.ok && data.ok !== false && !data.error && Boolean(data.online ?? true), models, error: typeof data.error === 'string' ? data.error : typeof data.ping_error === 'string' ? data.ping_error : typeof data.detail === 'string' ? data.detail : null };
}

/** The combo a key event stands for, in the app's `ctrl+alt+x` spelling. */
export function comboFromEvent(e: KeyboardEvent): string | null {
  const key = e.key.toLowerCase();
  if (['control', 'alt', 'shift', 'meta'].includes(key)) return null;
  const parts: string[] = [];
  if (e.ctrlKey || e.metaKey) parts.push('ctrl');
  if (e.altKey) parts.push('alt');
  if (e.shiftKey) parts.push('shift');
  parts.push(key === ' ' ? 'space' : key);
  return parts.join('+');
}

export const KEYBIND_LABELS: Record<string, string> = {
  search: 'Search conversations',
  toggle_sidebar: 'Show or hide the sidebar',
  new_session: 'New conversation',
  fav_session: 'Mark as favourite',
  delete_session: 'Delete the conversation (twice)',
  cancel: 'Cancel / stop',
  tts: 'Read the last reply',
  incognito: 'Incognito mode',
  settings: 'Settings',
  focus_input: 'Go to the composer',
  open_calendar: 'Open the calendar',
  open_compare: 'Open Compare',
  open_cookbook: 'Open the Cookbook',
  open_research: 'Open Deep Research',
  open_gallery: 'Open the images',
  open_library: 'Open the Library',
  open_memory: 'Open the Memory',
  open_notes: 'Open the Notes',
  open_tasks: 'Open the Automations',
  open_theme: 'Open the theme',
};
