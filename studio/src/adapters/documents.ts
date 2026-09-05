import { ApiError, asArray, getJson } from './api';
import { t } from '../i18n';

/**
 * Living documents (routes/document): the agent creates and edits them, the
 * person reads, edits, versions and exports them. The side panel's editor
 * uses exactly the endpoints the legacy editor uses.
 */

export interface Doc {
  id: string;
  sessionId: string | null;
  title: string;
  language: string;
  content: string;
  versionCount: number;
  archived: boolean;
  updatedAt: string | null;
}

export interface DocVersion {
  id: string;
  number: number;
  content: string;
  summary: string;
  source: string;
  createdAt: string | null;
}

function docFrom(raw: Record<string, unknown>): Doc {
  return {
    id: String(raw.id ?? ''),
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : null,
    title: String(raw.title ?? t('Untitled')),
    language: String(raw.language ?? ''),
    content: String(raw.current_content ?? raw.content ?? ''),
    versionCount: typeof raw.version_count === 'number' ? raw.version_count : 1,
    archived: Boolean(raw.archived),
    updatedAt: typeof raw.updated_at === 'string' ? raw.updated_at : null,
  };
}

async function send(path: string, method: string, body?: unknown): Promise<Record<string, unknown>> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try {
      detail = String(((await response.json()) as { detail?: unknown }).detail ?? '');
    } catch {
      detail = '';
    }
    throw new ApiError(detail || `${path} responded ${response.status}`, response.status);
  }
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

const enc = encodeURIComponent;

export async function getDoc(id: string, signal?: AbortSignal): Promise<Doc> {
  return docFrom(await getJson<Record<string, unknown>>(`/api/document/${enc(id)}`, signal));
}

export async function createDoc(input: { title: string; language?: string; content?: string; sessionId?: string | null }): Promise<Doc> {
  return docFrom(
    await send('/api/document', 'POST', {
      title: input.title,
      language: input.language ?? null,
      content: input.content ?? '',
      session_id: input.sessionId ?? null,
    }),
  );
}

/** Saves the content; the server coalesces quick successive saves into one
 *  version unless `forceVersion`. */
export async function saveDoc(id: string, content: string, summary?: string, forceVersion = false): Promise<Doc> {
  return docFrom(await send(`/api/document/${enc(id)}`, 'PUT', { content, summary: summary ?? null, force_version: forceVersion }));
}

export async function renameDoc(id: string, title: string, language?: string): Promise<Doc> {
  return docFrom(await send(`/api/document/${enc(id)}`, 'PATCH', { title, language: language ?? null }));
}

export async function archiveDoc(id: string): Promise<void> {
  await send(`/api/document/${enc(id)}/archive`, 'POST');
}

export async function deleteDoc(id: string): Promise<void> {
  await send(`/api/document/${enc(id)}`, 'DELETE');
}

export async function listDocVersions(id: string): Promise<DocVersion[]> {
  const raw = await getJson<unknown>(`/api/document/${enc(id)}/versions`);
  return asArray<Record<string, unknown>>(raw).map((v) => ({
    id: String(v.id ?? ''),
    number: typeof v.version_number === 'number' ? v.version_number : 0,
    content: String(v.content ?? ''),
    summary: String(v.summary ?? ''),
    source: String(v.source ?? ''),
    createdAt: typeof v.created_at === 'string' ? v.created_at : null,
  }));
}

export async function restoreDocVersion(id: string, number: number): Promise<Doc> {
  return docFrom(await send(`/api/document/${enc(id)}/restore/${number}`, 'POST'));
}

export function docPdfUrl(id: string): string {
  return `/api/document/${enc(id)}/export-pdf`;
}
