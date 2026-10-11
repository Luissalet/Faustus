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
  turnId: string;
  source: string;
  undone: boolean;
  /** The text was erased (the memory was forgotten for good): kept as a stub. */
  purged: boolean;
  /** Only the start of a very long text was kept, so it cannot be undone. */
  truncated: boolean;
}

interface RawChange {
  id: string; ts: number; store: MemoryStore; op: MemoryOp; item_id: string; label?: string;
  before_label?: string; session_id?: string; turn_id?: string; source?: string; undone?: boolean;
  purged?: boolean; truncated?: boolean;
}

export function changeFrom(raw: RawChange): MemoryChange {
  return {
    id: raw.id, ts: Number(raw.ts) || 0, store: raw.store, op: raw.op, itemId: raw.item_id,
    label: raw.label ?? '', beforeLabel: raw.before_label ?? '', sessionId: raw.session_id ?? '',
    turnId: raw.turn_id ?? '', source: raw.source ?? '', undone: !!raw.undone, purged: !!raw.purged,
    truncated: !!raw.truncated,
  };
}

export async function listMemoryChanges(
  filter: { sessionId?: string; turnId?: string; store?: MemoryStore | '' } = {},
  signal?: AbortSignal,
): Promise<MemoryChange[]> {
  const q = new URLSearchParams({ limit: '200' });
  if (filter.sessionId) q.set('session_id', filter.sessionId);
  if (filter.turnId) q.set('turn_id', filter.turnId);
  if (filter.store) q.set('store', filter.store);
  const body = await getJson<{ changes?: RawChange[] }>(`/api/memory-journal/changes?${q}`, signal);
  return (body.changes ?? []).map(changeFrom);
}

/** Why an undo was refused: the server's stable `code` plus its details, so
 *  the screen can say it in the interface language. */
export class MemoryUndoError extends ApiError {
  constructor(
    message: string,
    status: number,
    readonly code: string,
    readonly blocking?: { id: string; op: MemoryOp; label: string },
  ) {
    super(message, status);
    this.name = 'MemoryUndoError';
  }
}

export async function undoMemoryChange(id: string): Promise<void> {
  const path = `/api/memory-journal/changes/${encodeURIComponent(id)}/undo`;
  const response = await fetch(path, { method: 'POST', credentials: 'same-origin' });
  if (response.ok) return;
  let code = '';
  let blocking: MemoryUndoError['blocking'];
  try {
    const body = (await response.clone().json()) as { detail?: { code?: unknown; blocking?: MemoryUndoError['blocking'] } };
    if (body?.detail && typeof body.detail === 'object') {
      code = typeof body.detail.code === 'string' ? body.detail.code : '';
      blocking = body.detail.blocking;
    }
  } catch {
    /* not JSON: the status line below says it */
  }
  throw new MemoryUndoError(await responseReason(response, path), response.status, code, blocking);
}

/** Delete the caller's journal («clear history»). The memories stay. */
export async function clearMemoryChanges(): Promise<number> {
  const path = '/api/memory-journal/changes';
  const response = await fetch(path, { method: 'DELETE', credentials: 'same-origin' });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  const body = (await response.json().catch(() => ({}))) as { deleted?: number };
  return Number(body.deleted) || 0;
}
