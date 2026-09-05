import { ApiError, getJson } from './api';
import { t, locale } from '../i18n';

/**
 * Notas: the same `/api/notes` the previous interface's notes.js talks to.
 * Nothing is reshaped on the server; the screen speaks the note as stored
 * (type, colour, label, checklist items, due date with repeat, pin, archive,
 * manual order).
 */

export type NoteType = 'note' | 'todo' | 'goal' | 'draw' | 'checklist';

export interface NoteItem {
  id?: string;
  text: string;
  done: boolean;
  agent_status?: string;
}

export interface Note {
  id: string;
  title: string;
  content: string;
  items: NoteItem[] | null;
  noteType: NoteType;
  /** '' | red | orange | yellow | green | blue | purple | 'bg:<url>' */
  color: string;
  label: string;
  pinned: boolean;
  archived: boolean;
  dueDate: string | null;
  repeat: string;
  imageUrl: string | null;
  sortOrder: number;
  source: string;
  sessionId: string | null;
  agentSessionId: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export const NOTE_COLORS = ['', 'red', 'orange', 'yellow', 'green', 'blue', 'purple'] as const;

function fromServer(raw: Record<string, unknown>): Note {
  const items = Array.isArray(raw.items)
    ? (raw.items as Record<string, unknown>[]).map((it) => ({
        id: typeof it.id === 'string' ? it.id : undefined,
        text: typeof it.text === 'string' ? it.text : '',
        done: Boolean(it.done),
        agent_status: typeof it.agent_status === 'string' ? it.agent_status : undefined,
      }))
    : null;
  return {
    id: String(raw.id),
    title: typeof raw.title === 'string' ? raw.title : '',
    content: typeof raw.content === 'string' ? raw.content : '',
    items,
    noteType: (typeof raw.note_type === 'string' ? raw.note_type : 'note') as NoteType,
    color: typeof raw.color === 'string' ? raw.color : '',
    label: typeof raw.label === 'string' ? raw.label : '',
    pinned: Boolean(raw.pinned),
    archived: Boolean(raw.archived),
    dueDate: typeof raw.due_date === 'string' && raw.due_date ? raw.due_date : null,
    repeat: typeof raw.repeat === 'string' && raw.repeat ? raw.repeat : 'none',
    imageUrl: typeof raw.image_url === 'string' && raw.image_url ? raw.image_url : null,
    sortOrder: typeof raw.sort_order === 'number' ? raw.sort_order : 0,
    source: typeof raw.source === 'string' ? raw.source : 'user',
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : null,
    agentSessionId: typeof raw.agent_session_id === 'string' ? raw.agent_session_id : null,
    createdAt: typeof raw.created_at === 'string' ? raw.created_at : null,
    updatedAt: typeof raw.updated_at === 'string' ? raw.updated_at : null,
  };
}

export interface NoteDraft {
  title?: string;
  content?: string;
  items?: NoteItem[] | null;
  noteType?: NoteType;
  color?: string;
  label?: string;
  pinned?: boolean;
  archived?: boolean;
  dueDate?: string | null;
  repeat?: string;
  imageUrl?: string | null;
  sortOrder?: number;
  agentSessionId?: string | null;
}

function toServer(draft: NoteDraft): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (draft.title !== undefined) out.title = draft.title;
  if (draft.content !== undefined) out.content = draft.content;
  if (draft.items !== undefined) out.items = draft.items;
  if (draft.noteType !== undefined) out.note_type = draft.noteType;
  if (draft.color !== undefined) out.color = draft.color;
  if (draft.label !== undefined) out.label = draft.label;
  if (draft.pinned !== undefined) out.pinned = draft.pinned;
  if (draft.archived !== undefined) out.archived = draft.archived;
  // The server treats null as "leave alone"; an empty string clears it.
  if (draft.dueDate !== undefined) out.due_date = draft.dueDate ?? '';
  if (draft.repeat !== undefined) out.repeat = draft.repeat;
  if (draft.imageUrl !== undefined) out.image_url = draft.imageUrl ?? '';
  if (draft.sortOrder !== undefined) out.sort_order = draft.sortOrder;
  if (draft.agentSessionId !== undefined) out.agent_session_id = draft.agentSessionId ?? '';
  return out;
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(`${path} responded ${response.status}`, response.status);
  return (await response.json()) as T;
}

export async function listNotes(archived = false, signal?: AbortSignal): Promise<Note[]> {
  const data = await getJson<{ notes?: Record<string, unknown>[] }>(`/api/notes?archived=${archived ? 'true' : 'false'}`, signal);
  return (data.notes ?? []).map(fromServer);
}

export async function createNote(draft: NoteDraft): Promise<Note> {
  return fromServer(await send<Record<string, unknown>>('/api/notes', 'POST', { note_type: 'note', ...toServer(draft) }));
}

export async function updateNote(id: string, draft: NoteDraft): Promise<Note> {
  return fromServer(await send<Record<string, unknown>>(`/api/notes/${encodeURIComponent(id)}`, 'PUT', toServer(draft)));
}

export async function deleteNote(id: string): Promise<void> {
  await send(`/api/notes/${encodeURIComponent(id)}`, 'DELETE');
}

export async function togglePin(id: string): Promise<boolean> {
  return (await send<{ pinned: boolean }>(`/api/notes/${encodeURIComponent(id)}/pin`, 'POST')).pinned;
}

export async function toggleArchive(id: string): Promise<boolean> {
  return (await send<{ archived: boolean }>(`/api/notes/${encodeURIComponent(id)}/archive`, 'POST')).archived;
}

export async function toggleItem(id: string, index: number): Promise<NoteItem[]> {
  return (await send<{ items: NoteItem[] }>(`/api/notes/${encodeURIComponent(id)}/items/${index}/toggle`, 'POST')).items;
}

export async function reorderNotes(ids: string[]): Promise<void> {
  await send('/api/notes/reorder', 'POST', { ids });
}

export interface ReminderResult {
  synthesis: string | null;
  email_sent: boolean;
  ntfy_sent: boolean;
  webhook_sent: boolean;
  browser_sent: boolean;
}

/** The server fans the reminder out (mail, ntfy, webhook, AI synthesis) as the settings say. */
export async function fireReminder(noteId: string): Promise<ReminderResult> {
  return send<ReminderResult>('/api/notes/fire-reminder', 'POST', { note_id: noteId });
}

/* ── Dates, repeats ─────────────────────────────────────────────────────── */

export function parseDue(value: string | null): Date | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function hasTime(value: string | null): boolean {
  return Boolean(value && /T\d{2}:\d{2}/.test(value));
}

export function formatDue(value: string | null): string {
  const d = parseDue(value);
  if (!d) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  const day = sameDay ? t('today') : d.toDateString() === tomorrow.toDateString() ? t('tomorrow') : d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' });
  if (!hasTime(value)) return day;
  return `${day} ${d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' })}`;
}

export function isOverdue(value: string | null): boolean {
  const d = parseDue(value);
  return Boolean(d && d.getTime() < Date.now());
}

export function isToday(value: string | null): boolean {
  const d = parseDue(value);
  return Boolean(d && d.toDateString() === new Date().toDateString());
}

/** `datetime-local` wants local time without zone, to the minute. */
export function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export const REPEATS: { value: string; label: string }[] = [
  { value: 'none', label: 'No repeat' },
  { value: 'daily', label: 'Every day' },
  { value: 'weekly', label: 'Every week' },
  { value: 'monthly', label: 'Every month' },
  { value: 'yearly', label: 'Every year' },
];

export function repeatLabel(repeat: string): string {
  if (!repeat || repeat === 'none') return '';
  const head = repeat.split(':')[0];
  const found = REPEATS.find((r) => r.value === head);
  return found ? t(found.label) : repeat;
}

/**
 * Next occurrence after `from` for a stored repeat (`daily`, `weekly[:wd]`,
 * `monthly[:day:N|:nth:N:wd|:last:wd]`, `yearly`). Same rules as the previous
 * interface's `_advanceRecurring`, reduced to what the stored shapes need.
 */
export function advance(value: string, repeat: string): string | null {
  const d = parseDue(value);
  if (!d || !repeat || repeat === 'none') return null;
  const parts = repeat.split(':');
  const kind = parts[0];
  const withTime = hasTime(value);
  const next = new Date(d);
  const now = Date.now();
  let guard = 0;
  while (next.getTime() <= now && guard++ < 1000) {
    if (kind === 'daily') next.setDate(next.getDate() + 1);
    else if (kind === 'weekly') next.setDate(next.getDate() + 7);
    else if (kind === 'monthly') {
      if (parts[1] === 'nth' || parts[1] === 'last') {
        const wd = Number(parts[parts.length - 1]);
        const n = parts[1] === 'nth' ? Number(parts[2]) : -1;
        const y = next.getMonth() === 11 ? next.getFullYear() + 1 : next.getFullYear();
        const m = (next.getMonth() + 1) % 12;
        if (n > 0) {
          const first = new Date(y, m, 1);
          const offset = (wd - first.getDay() + 7) % 7;
          next.setFullYear(y, m, 1 + offset + (n - 1) * 7);
        } else {
          const last = new Date(y, m + 1, 0);
          const back = (last.getDay() - wd + 7) % 7;
          next.setFullYear(y, m, last.getDate() - back);
        }
      } else {
        const want = parts[1] === 'day' ? Number(parts[2]) : d.getDate();
        const y = next.getMonth() === 11 ? next.getFullYear() + 1 : next.getFullYear();
        const m = (next.getMonth() + 1) % 12;
        const lastDay = new Date(y, m + 1, 0).getDate();
        next.setFullYear(y, m, Math.min(want, lastDay));
      }
    } else if (kind === 'yearly') next.setFullYear(next.getFullYear() + 1);
    else return null;
  }
  return withTime ? toLocalInput(next) : toLocalInput(next).slice(0, 10);
}

export function isChecklist(note: Pick<Note, 'noteType'>): boolean {
  return note.noteType === 'todo' || note.noteType === 'goal' || note.noteType === 'checklist';
}

export function progress(note: Note): { done: number; total: number } {
  const items = note.items ?? [];
  return { done: items.filter((i) => i.done).length, total: items.length };
}

export function labelsOf(note: Pick<Note, 'label'>): string[] {
  return note.label
    .split(/[\s,]+/)
    .map((l) => l.replace(/^#/, '').trim())
    .filter(Boolean);
}
