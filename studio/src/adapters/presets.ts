import { asArray, getJson } from './api';

/**
 * Presets: a system prompt plus sampling defaults, picked per message
 * (`preset_id` on /api/chat_stream). Built-in ones come from
 * /api/presets; the user's own templates from /api/presets/templates.
 */

export interface Preset {
  id: string;
  name: string;
  systemPrompt: string;
  temperature?: number;
  maxTokens?: number;
  /** true for a template the user wrote (deletable). */
  own: boolean;
}

export async function listPresets(signal?: AbortSignal): Promise<Preset[]> {
  const [builtin, templates] = await Promise.all([
    getJson<Record<string, Record<string, unknown>>>('/api/presets', signal).catch(() => ({}) as Record<string, Record<string, unknown>>),
    getJson<unknown>('/api/presets/templates', signal).catch(() => []),
  ]);
  const out: Preset[] = [];
  for (const [id, p] of Object.entries(builtin ?? {})) {
    if (!p || typeof p !== 'object') continue;
    if (p.enabled === false) continue;
    out.push({
      id,
      name: String(p.name ?? id),
      systemPrompt: String(p.system_prompt ?? ''),
      temperature: typeof p.temperature === 'number' ? p.temperature : undefined,
      maxTokens: typeof p.max_tokens === 'number' ? p.max_tokens : undefined,
      own: false,
    });
  }
  for (const t of asArray<Record<string, unknown>>(templates, 'templates')) {
    const id = String(t.id ?? '');
    if (!id) continue;
    out.push({ id, name: String(t.name ?? id), systemPrompt: String(t.system_prompt ?? ''), temperature: typeof t.temperature === 'number' ? t.temperature : undefined, maxTokens: typeof t.max_tokens === 'number' && t.max_tokens > 0 ? t.max_tokens : undefined, own: true });
  }
  return out;
}

export async function saveTemplate(input: { id?: string; name: string; systemPrompt: string; temperature?: number; maxTokens?: number }): Promise<Preset> {
  const response = await fetch('/api/presets/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ id: input.id ?? '', name: input.name, system_prompt: input.systemPrompt, temperature: input.temperature ?? 1.0, max_tokens: input.maxTokens ?? 0 }),
  });
  if (!response.ok) throw new Error(`templates responded ${response.status}`);
  const raw = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  const t = (raw.template && typeof raw.template === 'object' ? raw.template : raw) as Record<string, unknown>;
  return { id: String(t.id ?? input.id ?? ''), name: String(t.name ?? input.name), systemPrompt: String(t.system_prompt ?? input.systemPrompt), own: true };
}

export async function deleteTemplate(id: string): Promise<void> {
  const response = await fetch(`/api/presets/templates/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' });
  if (!response.ok) throw new Error(`templates responded ${response.status}`);
}

/** Rough notes → a full system prompt, written by the model (`/api/presets/expand`). */
export async function expandPrompt(name: string, draft: string, model = ''): Promise<string> {
  const response = await fetch('/api/presets/expand', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ name, prompt: draft, model }),
  });
  if (!response.ok) throw new Error(`expand responded ${response.status}`);
  const data = (await response.json()) as { success?: boolean; prompt?: string; message?: string };
  if (!data.success || !data.prompt) throw new Error(data.message || 'Nothing came back');
  return data.prompt;
}

export interface CustomPersona {
  name: string;
  enabled: boolean;
  temperature: number;
  maxTokens: number;
  systemPrompt: string;
  injectPrefix: string;
  injectSuffix: string;
}

/** The ad-hoc persona (`custom` preset): edited in place, no template needed. */
export async function getCustomPersona(signal?: AbortSignal): Promise<CustomPersona> {
  const all = await getJson<Record<string, Record<string, unknown>>>('/api/presets', signal).catch(() => ({}) as Record<string, Record<string, unknown>>);
  const c = all.custom ?? {};
  return {
    name: String(c.character_name ?? (c.name === 'Custom' ? '' : c.name) ?? ''),
    enabled: c.enabled !== false,
    temperature: typeof c.temperature === 'number' ? c.temperature : 1,
    maxTokens: typeof c.max_tokens === 'number' ? c.max_tokens : 0,
    systemPrompt: String(c.system_prompt ?? ''),
    injectPrefix: String(c.inject_prefix ?? ''),
    injectSuffix: String(c.inject_suffix ?? ''),
  };
}

export async function saveCustomPersona(p: CustomPersona): Promise<void> {
  const response = await fetch('/api/presets/custom', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ name: p.name, enabled: p.enabled, temperature: p.temperature, max_tokens: p.maxTokens, system_prompt: p.systemPrompt, inject_prefix: p.injectPrefix, inject_suffix: p.injectSuffix }),
  });
  if (!response.ok) throw new Error(`custom responded ${response.status}`);
  const data = (await response.json().catch(() => ({}))) as { success?: boolean; message?: string };
  if (data.success === false) throw new Error(data.message || 'Failed');
}
