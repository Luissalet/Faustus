import { ApiError } from './api';

/**
 * Session and message actions. Each one is a legacy endpoint the old sidebar
 * or message toolbar already calls (`static/js/sessions.js`, `chat.js`), so
 * a conversation edited here reads identically when opened over there.
 */

async function check(response: Response, what: string): Promise<Response> {
  if (response.ok) return response;
  let detail = '';
  try {
    detail = String(((await response.json()) as { detail?: unknown }).detail ?? '');
  } catch {
    detail = '';
  }
  throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
}

async function postJson(path: string, body: unknown, what: string): Promise<unknown> {
  const response = await check(
    await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body),
    }),
    what,
  );
  try {
    return await response.json();
  } catch {
    return null;
  }
}

const sid = (id: string) => encodeURIComponent(id);

/* ── Sessions ── */

export async function renameSession(id: string, name: string): Promise<void> {
  const fd = new FormData();
  fd.append('name', name);
  await check(
    await fetch(`/api/session/${sid(id)}`, { method: 'PATCH', body: fd, credentials: 'same-origin' }),
    'rename',
  );
}

export async function setSessionImportant(id: string, important: boolean): Promise<void> {
  const fd = new FormData();
  fd.append('important', important ? 'true' : 'false');
  await check(
    await fetch(`/api/session/${sid(id)}/important`, {
      method: 'POST',
      body: fd,
      credentials: 'same-origin',
    }),
    'important',
  );
}

export async function archiveSession(id: string): Promise<void> {
  await check(
    await fetch(`/api/session/${sid(id)}/archive`, { method: 'POST', credentials: 'same-origin' }),
    'archive',
  );
}

export async function deleteSession(id: string): Promise<void> {
  await check(
    await fetch(`/api/session/${sid(id)}`, { method: 'DELETE', credentials: 'same-origin' }),
    'delete',
  );
}

export const EXPORT_FORMATS = ['md', 'txt', 'json', 'html', 'docx', 'pdf'] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

export function exportUrl(id: string, fmt: ExportFormat): string {
  return `/api/session/${sid(id)}/export?fmt=${fmt}`;
}

export async function compactSession(id: string): Promise<{ compacted?: boolean; detail?: string }> {
  const result = (await postJson(`/api/session/${sid(id)}/compact`, {}, 'compact')) as
    | Record<string, unknown>
    | null;
  return {
    compacted: result ? result.compacted !== false : true,
    detail: result && typeof result.message === 'string' ? result.message : undefined,
  };
}

/* ── Messages ── */

/** Keeps the first `keepCount` messages; a version is saved server-side. */
export async function truncateSession(
  id: string,
  keepCount: number,
  reason: 'edit' | 'regenerate' | 'truncate',
): Promise<void> {
  await postJson(`/api/session/${sid(id)}/truncate`, { keep_count: keepCount, reason }, 'truncate');
}

export async function deleteMessages(id: string, msgIds: string[]): Promise<void> {
  await postJson(`/api/session/${sid(id)}/delete-messages`, { msg_ids: msgIds }, 'delete-messages');
}

export async function editMessage(id: string, msgId: string, content: string): Promise<void> {
  await postJson(`/api/session/${sid(id)}/edit-message`, { msg_id: msgId, content }, 'edit-message');
}

export interface ChatVersion {
  id: string;
  createdAt: string;
  reason: string;
  removed: number;
}

export async function listVersions(id: string): Promise<ChatVersion[]> {
  const response = await check(
    await fetch(`/api/session/${sid(id)}/versions`, { credentials: 'same-origin' }),
    'versions',
  );
  const raw = (await response.json()) as { versions?: unknown };
  const list = Array.isArray(raw.versions) ? raw.versions : Array.isArray(raw) ? raw : [];
  return (list as Record<string, unknown>[]).map((v) => ({
    id: String(v.id ?? v.version_id ?? ''),
    createdAt: String(v.created_at ?? v.saved_at ?? ''),
    reason: String(v.reason ?? ''),
    removed: typeof v.removed === 'number' ? v.removed : Number(v.message_count ?? 0),
  }));
}

export async function restoreVersion(id: string, versionId: string): Promise<void> {
  await postJson(`/api/session/${sid(id)}/versions/${encodeURIComponent(versionId)}/restore`, {}, 'restore');
}
