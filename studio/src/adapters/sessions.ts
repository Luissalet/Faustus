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

/* ── Fork, folders, bulk actions, archive ── */

/** A new conversation with the first `keepCount` messages of this one. */
export async function forkSession(id: string, keepCount: number): Promise<{ id: string; name: string }> {
  const raw = (await postJson(`/api/session/${sid(id)}/fork`, { keep_count: keepCount }, 'fork')) as Record<string, unknown> | null;
  if (!raw || typeof raw.id !== 'string') throw new ApiError('fork returned no id', 500);
  return { id: raw.id, name: typeof raw.name === 'string' ? raw.name : '' };
}

export async function setSessionFolder(id: string, folder: string): Promise<void> {
  const fd = new FormData();
  fd.append('folder', folder);
  await check(await fetch(`/api/session/${sid(id)}`, { method: 'PATCH', body: fd, credentials: 'same-origin' }), 'folder');
}

export async function bulkDeleteSessions(ids: string[]): Promise<number> {
  const raw = (await postJson('/api/sessions/bulk-delete', { ids }, 'bulk-delete')) as Record<string, unknown> | null;
  return raw && typeof raw.deleted === 'number' ? raw.deleted : ids.length;
}

export async function unarchiveSession(id: string): Promise<void> {
  await check(await fetch(`/api/session/${sid(id)}/unarchive`, { method: 'POST', credentials: 'same-origin' }), 'unarchive');
}

export interface ArchivedSession {
  id: string;
  name: string;
  model: string;
  messageCount: number;
  lastMessageAt: string | null;
  archivedAt: string | null;
}

export async function listArchivedSessions(search = '', offset = 0, limit = 40): Promise<{ sessions: ArchivedSession[]; total: number }> {
  const q = new URLSearchParams({ search, offset: String(offset), limit: String(limit) });
  const response = await check(await fetch(`/api/sessions/archived?${q}`, { credentials: 'same-origin' }), 'archived');
  const raw = (await response.json()) as { sessions?: unknown; total?: unknown } | unknown[];
  const list = Array.isArray(raw) ? raw : Array.isArray((raw as { sessions?: unknown }).sessions) ? ((raw as { sessions: unknown[] }).sessions) : [];
  const sessions = (list as Record<string, unknown>[]).map((s) => ({
    id: String(s.id ?? ''),
    name: String(s.name ?? 'Sin título'),
    model: String(s.model ?? ''),
    messageCount: typeof s.message_count === 'number' ? s.message_count : 0,
    lastMessageAt: typeof s.last_message_at === 'string' ? s.last_message_at : null,
    archivedAt: typeof s.archived_at === 'string' ? s.archived_at : null,
  }));
  const total = !Array.isArray(raw) && typeof raw.total === 'number' ? raw.total : sessions.length;
  return { sessions, total };
}

export interface AutoSortResult {
  status: string;
  updated: number;
  deletedEmpty: number;
  deletedThrowaway: number;
  folders: string[];
  remaining: number;
  reason?: string;
}

/** Tidies (deletes empty/throwaway chats) and, unless `skipLlm`, files the
 *  rest into folders with the utility model. */
export async function autoSortSessions(skipLlm: boolean): Promise<AutoSortResult> {
  const raw = (await postJson(`/api/sessions/auto-sort${skipLlm ? '?skip_llm=true' : ''}`, {}, 'auto-sort')) as Record<string, unknown> | null;
  const r = raw ?? {};
  return {
    status: String(r.status ?? 'ok'),
    updated: typeof r.updated === 'number' ? r.updated : 0,
    deletedEmpty: typeof r.deleted_empty === 'number' ? r.deleted_empty : 0,
    deletedThrowaway: typeof r.deleted_throwaway === 'number' ? r.deleted_throwaway : 0,
    folders: Array.isArray(r.folders) ? r.folders.map(String) : [],
    remaining: typeof r.unfiled_remaining === 'number' ? r.unfiled_remaining : 0,
    reason: typeof r.reason === 'string' ? r.reason : undefined,
  };
}

/** One zip with one file per conversation (ids, or a whole folder). */
export function exportZipUrl(fmt: ExportFormat, opts: { ids?: string[]; folder?: string }): string {
  const q = new URLSearchParams({ fmt });
  if (opts.ids?.length) q.set('ids', opts.ids.join(','));
  if (opts.folder) q.set('folder', opts.folder);
  return `/api/sessions/export?${q}`;
}
