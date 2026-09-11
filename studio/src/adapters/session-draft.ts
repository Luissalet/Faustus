import { ApiError, getJson } from './api';

/**
 * UX-01's server-side half of the composer draft (`src/session_draft.py`):
 * one small `{text, attachment_ids, updated_at}` record per (owner,
 * session), so an unsent draft survives opening the same chat from another
 * device or browser profile — the gap Studio.tsx's own `localStorage` draft
 * cannot close by itself. `updated_at` is a Unix seconds timestamp
 * (`time.time()`), same as everywhere else the server hands one out.
 */
export interface SessionDraft {
  text: string;
  attachmentIds: string[];
  updatedAt: number;
}

export const emptySessionDraft: SessionDraft = { text: '', attachmentIds: [], updatedAt: 0 };

function fromWire(raw: { text?: unknown; attachment_ids?: unknown; updated_at?: unknown }): SessionDraft {
  return {
    text: typeof raw.text === 'string' ? raw.text : '',
    attachmentIds: Array.isArray(raw.attachment_ids) ? raw.attachment_ids.filter((id): id is string => typeof id === 'string') : [],
    updatedAt: typeof raw.updated_at === 'number' ? raw.updated_at : 0,
  };
}

export const loadSessionDraft = (sessionId: string, signal?: AbortSignal) =>
  getJson<{ text: string; attachment_ids: string[]; updated_at: number }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/draft`, signal,
  ).then(fromWire);

export async function saveSessionDraft(sessionId: string, text: string, attachmentIds: string[]): Promise<SessionDraft> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/draft`, {
    method: 'PUT',
    credentials: 'same-origin',
    signal: AbortSignal.timeout(20000),
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, attachment_ids: attachmentIds }),
  });
  if (!response.ok) {
    let detail = '';
    try { detail = String(((await response.json()) as { detail?: unknown }).detail ?? ''); } catch { /* status below */ }
    throw new ApiError(detail || `draft save responded ${response.status}`, response.status);
  }
  return fromWire(await response.json());
}
