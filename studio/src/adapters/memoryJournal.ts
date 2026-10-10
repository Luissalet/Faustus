import { ApiError, getJson, responseReason } from './api';

/**
 * Journal of memory changes (#242): `/api/memory-journal`. What the assistant
 * (or an extractor, or you) created, edited or deleted in the facts store,
 * the learned memory, the instincts and the notes, with an undo per change.
 */
export type MemoryStore = 'facts' | 'rules' | 'instincts' | 'notes';
export type MemoryOp = 'create' | 'edit' | 'delete';

export interface MemoryChange {
  id: string;
  ts: number;
  store: MemoryStore;
  op: MemoryOp;
  itemId: string;
  label: string;
  beforeLabel: string;
  sessionId: string;
  source: string;
  undone: boolean;
}

interface RawChange {
  id: string; ts: number; store: MemoryStore; op: MemoryOp; item_id: string; label?: string;
  before_label?: string; session_id?: string; source?: string; undone?: boolean;
}

export function changeFrom(raw: RawChange): MemoryChange {
  return {
    id: raw.id, ts: Number(raw.ts) || 0, store: raw.store, op: raw.op, itemId: raw.item_id,
    label: raw.label ?? '', beforeLabel: raw.before_label ?? '', sessionId: raw.session_id ?? '',
    source: raw.source ?? '', undone: !!raw.undone,
  };
}

export async function listMemoryChanges(filter: { sessionId?: string; store?: MemoryStore | '' } = {},
  signal?: AbortSignal): Promise<MemoryChange[]> {
  const q = new URLSearchParams({ limit: '200' });
  if (filter.sessionId) q.set('session_id', filter.sessionId);
  if (filter.store) q.set('store', filter.store);
  const body = await getJson<{ changes?: RawChange[] }>(`/api/memory-journal/changes?${q}`, signal);
  return (body.changes ?? []).map(changeFrom);
}

export async function undoMemoryChange(id: string): Promise<void> {
  const path = `/api/memory-journal/changes/${encodeURIComponent(id)}/undo`;
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin' });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
}
