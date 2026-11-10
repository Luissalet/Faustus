import { locale, t } from '../../i18n';
import type { AgoraGuild, AgoraMember, BoardColumn, Presence, Target } from '../../adapters/agora';

/** What the main pane shows: `ch:<slug>`, `v:<view>`, `t:<thread id>`, `room:<id>` or `room:new`. */
export type ViewKind = 'channel' | 'view' | 'thread' | 'room';
export interface View { kind: ViewKind; id: string }

export function parseView(raw: string | null | undefined): View {
  const text = String(raw || '');
  const cut = text.indexOf(':');
  const head = cut > 0 ? text.slice(0, cut) : '';
  const id = cut > 0 ? text.slice(cut + 1) : '';
  if (head === 'v' && id) return { kind: 'view', id };
  // (the inbox and a search are views too; they are not remembered as the last place of a server)
  if (head === 't' && /^\d+$/.test(id)) return { kind: 'thread', id };
  if (head === 'room' && id) return { kind: 'room', id };
  if (head === 'ch' && id) return { kind: 'channel', id };
  return { kind: 'channel', id: 'general' };
}
export const viewKey = (view: View) => ({ channel: 'ch', view: 'v', thread: 't', room: 'room' })[view.kind] + ':' + view.id;
export const viewTarget = (view: View): Target | null =>
  view.kind === 'channel' ? { channel: view.id } : view.kind === 'thread' ? { threadId: Number(view.id) } : null;

/** One colour per provider; an agent id is the provider or starts with `<provider>-`. */
export const ROLE_COLORS: Record<string, string> = {
  person: '#e2b24c', claude: '#d97757', codex: '#10a37f', cursor: '#7aa2f7', faustus: '#d0545b', other: '#9aa3b2', system: '#8c929e',
};
export const ROLE_ORDER = ['person', 'claude', 'codex', 'cursor', 'faustus', 'other'];
export const ROLE_LABEL: Record<string, string> = {
  person: 'You', claude: 'Claude', codex: 'Codex', cursor: 'Cursor', faustus: 'Faustus', other: 'Others',
};
export function roleOf(agent: string | null | undefined): string {
  const id = String(agent || '').toLowerCase();
  if (id === 'luis') return 'person';
  if (id === 'system') return 'system';
  for (const role of ['claude', 'codex', 'cursor', 'faustus']) if (id === role || id.startsWith(role + '-')) return role;
  return 'other';
}
export const agentColor = (agent: string | null | undefined) => ROLE_COLORS[roleOf(agent)] ?? ROLE_COLORS.other;
export function agentInitials(agent: string | null | undefined): string {
  const parts = String(agent || '?').split(/[-_.\s]+/).filter(Boolean);
  const first = parts[0]?.[0] ?? '?';
  const second = parts.length > 1 ? parts[parts.length - 1][0] : parts[0]?.[1] ?? '';
  return (first + second).toUpperCase();
}

export const PRESENCE_LABEL: Record<Presence, string> = {
  person: 'Here', working: 'At work', idle: 'Idle', waiting: 'Waiting', away: 'No heartbeat lately',
  offline: 'Offline', no_quota: 'Out of quota', invited: 'Has not come in yet',
};
export const isOnline = (m: AgoraMember) => m.presence !== 'offline';
/** A size a person reads: 3,4 MB, 120 KB. */
export function fileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toLocaleString(locale(), { maximumFractionDigits: 1 }) + ' MB';
  if (bytes >= 1024) return Math.round(bytes / 1024).toLocaleString(locale()) + ' KB';
  return bytes + ' B';
}

export const COLUMN_LABEL: Record<BoardColumn, string> = { open: 'To do', in_progress: 'In progress', review: 'In review', done: 'Done' };
export const STATUS_LABEL: Record<string, string> = {
  open: 'Open#issue_status', claimed: 'Claimed', in_progress: 'In progress', blocked: 'Blocked#issue_status', changes: 'Changes asked',
  review: 'In review', approved: 'Approved', done: 'Done#issue_status', dropped: 'Dropped task',
};
export const KIND_LABEL: Record<string, string> = {
  comment: '', proposal: 'Proposal', agree: 'Agrees', disagree: 'Disagrees', approve: 'Approves', changes: 'Asks for changes',
  resolution: 'Decision', escalation: 'Question for you', system: '',
};
export const THREAD_KIND_LABEL: Record<string, string> = {
  debate: 'Debate', question: 'Question', decision: 'Decision', review: 'Review', handoff: 'Handoff', note: 'Note', task: 'Task',
};

export function clock(ts: number | null | undefined): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' }) : '';
}
export function dayLabel(ts: number): string {
  const day = new Date(ts * 1000);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  if (day.toDateString() === today.toDateString()) return t('Today');
  if (day.toDateString() === yesterday.toDateString()) return t('Yesterday');
  return day.toLocaleDateString(locale(), { weekday: 'long', day: 'numeric', month: 'long', ...(day.getFullYear() !== today.getFullYear() ? { year: 'numeric' } : {}) });
}
export function stamp(ts: number | null | undefined): string {
  if (!ts) return '';
  const day = new Date(ts * 1000);
  const same = day.toDateString() === new Date().toDateString();
  return same ? clock(ts) : day.toLocaleDateString(locale(), { day: '2-digit', month: '2-digit' }) + ' ' + clock(ts);
}
export function ago(ts: number | null | undefined): string {
  if (!ts) return t('never');
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return t('just now');
  if (s < 3600) return t('{n} min ago', { n: Math.round(s / 60) });
  if (s < 86400) return t('{n} h ago', { n: Math.round(s / 3600) });
  return t('{n} d ago', { n: Math.round(s / 86400) });
}

/** A task of the loaded server by id, in whatever column it is. */
export function findTask(guild: AgoraGuild | null, id: number) {
  if (!guild) return null;
  for (const column of Object.values(guild.tasks)) {
    const hit = column.find((task) => task.id === id);
    if (hit) return hit;
  }
  return null;
}
export function findTaskByThread(guild: AgoraGuild | null, threadId: number) {
  if (!guild) return null;
  for (const column of Object.values(guild.tasks)) {
    const hit = column.find((task) => task.thread_id === threadId);
    if (hit) return hit;
  }
  return null;
}

/** Small bus so the member list can put `@id` in whatever composer is open. */
type Listener = (text: string) => void;
const listeners = new Set<Listener>();
export const composerBus = {
  insert(text: string) { for (const fn of listeners) fn(text); },
  listen(fn: Listener) { listeners.add(fn); return () => { listeners.delete(fn); }; },
};

export function remember(key: string, value: string): void {
  try { window.localStorage.setItem(key, value); } catch { /* private mode */ }
}
export function recall(key: string): string {
  try { return window.localStorage.getItem(key) || ''; } catch { return ''; }
}

/** Sent on `window` when what waits for the person changed by their own hand (a server muted or heard again), so the
 *  count on the sidebar does not wait for its next look. */
export const AGORA_WAITING_CHANGED = 'faustus:agora-waiting';
