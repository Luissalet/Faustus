/** Official API entry points. Credentials and model inventories are never bundled. */
export const PROVIDER_PRESETS = [
  {id: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com/v1', keyUrl: 'https://platform.openai.com/api-keys'},
  {id: 'claude', label: 'Claude', baseUrl: 'https://api.anthropic.com', keyUrl: 'https://platform.claude.com/settings/keys'},
  {id: 'gemini', label: 'Gemini', baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai', keyUrl: 'https://aistudio.google.com/apikey'},
] as const;

export function modelIds(raw: unknown): string[] {
  const rows = Array.isArray(raw) ? raw : raw && typeof raw === 'object' ? (raw as {models?: unknown}).models : [];
  if (!Array.isArray(rows)) return [];
  return [...new Set(rows.flatMap(row => {
    const id = typeof row === 'string' ? row : row && typeof row === 'object' ? row.id : null;
    return typeof id === 'string' && id.trim() ? [id.trim()] : [];
  }))];
}
