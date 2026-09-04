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
    out.push({ id, name: String(t.name ?? id), systemPrompt: String(t.system_prompt ?? ''), own: true });
  }
  return out;
}

export async function saveTemplate(input: { id?: string; name: string; systemPrompt: string }): Promise<Preset> {
  const response = await fetch('/api/presets/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ id: input.id ?? '', name: input.name, system_prompt: input.systemPrompt }),
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
