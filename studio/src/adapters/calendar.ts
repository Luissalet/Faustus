import { ApiError, getJson } from './api';
import { t, locale } from '../i18n';

/**
 * Calendario: `/api/calendar`, the same routes calendar.js uses. Events
 * come expanded (one per occurrence; an occurrence's uid is `base::stamp`),
 * timed ones in ISO with a trailing Z when the row is stored as UTC.
 */

export interface Calendar {
  id: string;
  name: string;
  color: string;
  source: 'local' | 'caldav' | string;
}

export interface CalEvent {
  uid: string;
  summary: string;
  /** All-day: YYYY-MM-DD; timed: ISO (Z when UTC). */
  dtstart: string;
  dtend: string;
  allDay: boolean;
  description: string;
  location: string;
  rrule: string;
  calendarId: string;
  calendarName: string;
  color: string;
  importance: string;
}

export interface EventDraft {
  summary: string;
  dtstart: string;
  dtend?: string | null;
  allDay: boolean;
  description?: string;
  location?: string;
  calendarId?: string | null;
  rrule?: string | null;
  color?: string | null;
}

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

function json(method: string, body: unknown): RequestInit {
  return { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

function eventFrom(raw: Record<string, unknown>): CalEvent {
  return {
    uid: String(raw.uid ?? ''),
    summary: typeof raw.summary === 'string' ? raw.summary : '',
    dtstart: String(raw.dtstart ?? ''),
    dtend: String(raw.dtend ?? raw.dtstart ?? ''),
    allDay: Boolean(raw.all_day),
    description: typeof raw.description === 'string' ? raw.description : '',
    location: typeof raw.location === 'string' ? raw.location : '',
    rrule: typeof raw.rrule === 'string' ? raw.rrule : '',
    calendarId: typeof raw.calendar_href === 'string' ? raw.calendar_href : '',
    calendarName: typeof raw.calendar === 'string' ? raw.calendar : '',
    color: typeof raw.color === 'string' ? raw.color : '',
    importance: typeof raw.importance === 'string' ? raw.importance : 'normal',
  };
}

export async function listCalendars(signal?: AbortSignal): Promise<Calendar[]> {
  const data = await getJson<{ calendars?: Record<string, unknown>[] }>('/api/calendar/calendars', signal);
  return (data.calendars ?? []).map((c) => ({
    id: String(c.href ?? ''),
    name: typeof c.name === 'string' ? c.name : 'Calendario',
    color: typeof c.color === 'string' && c.color ? c.color : '#5b8abf',
    source: typeof c.source === 'string' ? c.source : 'local',
  }));
}

/** `start`/`end` as YYYY-MM-DD (end exclusive). */
export async function listEvents(start: string, end: string, signal?: AbortSignal): Promise<{ events: CalEvent[]; truncated: boolean }> {
  const data = await getJson<{ events?: Record<string, unknown>[]; truncated?: boolean }>(`/api/calendar/events?start=${start}&end=${end}`, signal);
  return { events: (data.events ?? []).map(eventFrom), truncated: Boolean(data.truncated) };
}

function toServer(d: EventDraft): Record<string, unknown> {
  return {
    summary: d.summary,
    dtstart: d.dtstart,
    dtend: d.dtend ?? null,
    all_day: d.allDay,
    description: d.description ?? '',
    location: d.location ?? '',
    calendar_href: d.calendarId ?? null,
    rrule: d.rrule ?? null,
    color: d.color ?? null,
  };
}

export async function createEvent(draft: EventDraft): Promise<string> {
  const r = await ok(await fetch('/api/calendar/events', json('POST', toServer(draft))), 'calendar/events');
  return String(((await r.json()) as { uid?: string }).uid ?? '');
}

export async function updateEvent(uid: string, draft: Partial<EventDraft>): Promise<void> {
  const body: Record<string, unknown> = {};
  if (draft.summary !== undefined) body.summary = draft.summary;
  if (draft.dtstart !== undefined) body.dtstart = draft.dtstart;
  if (draft.dtend !== undefined) body.dtend = draft.dtend;
  if (draft.allDay !== undefined) body.all_day = draft.allDay;
  if (draft.description !== undefined) body.description = draft.description;
  if (draft.location !== undefined) body.location = draft.location;
  if (draft.rrule !== undefined) body.rrule = draft.rrule ?? '';
  if (draft.color !== undefined) body.color = draft.color ?? '';
  await ok(await fetch(`/api/calendar/events/${encodeURIComponent(uid)}`, json('PUT', body)), 'calendar/update');
}

export async function deleteEvent(uid: string, scope: 'series' | 'occurrence' = 'series'): Promise<void> {
  await ok(await fetch(`/api/calendar/events/${encodeURIComponent(uid)}?scope=${scope}`, { method: 'DELETE', credentials: 'same-origin' }), 'calendar/delete');
}

export async function createCalendar(name: string, color: string): Promise<Calendar> {
  const r = await ok(await fetch(`/api/calendar/calendars?name=${encodeURIComponent(name)}&color=${encodeURIComponent(color)}`, { method: 'POST', credentials: 'same-origin' }), 'calendar/calendars');
  const data = (await r.json()) as { id?: string; name?: string; color?: string };
  return { id: String(data.id ?? ''), name: data.name ?? name, color: data.color ?? color, source: 'local' };
}

export async function updateCalendar(id: string, name: string, color: string): Promise<void> {
  await ok(await fetch(`/api/calendar/calendars/${encodeURIComponent(id)}?name=${encodeURIComponent(name)}&color=${encodeURIComponent(color)}`, { method: 'PUT', credentials: 'same-origin' }), 'calendar/update-calendar');
}

export async function deleteCalendar(id: string): Promise<void> {
  await ok(await fetch(`/api/calendar/calendars/${encodeURIComponent(id)}`, { method: 'DELETE', credentials: 'same-origin' }), 'calendar/delete-calendar');
}

export function exportUrl(id: string): string {
  return `/api/calendar/export/${encodeURIComponent(id)}`;
}

export async function importIcs(file: File, calendarName = ''): Promise<{ imported: number; message: string }> {
  const fd = new FormData();
  fd.append('file', file);
  const q = calendarName ? `?calendar_name=${encodeURIComponent(calendarName)}` : '';
  const r = await ok(await fetch(`/api/calendar/import${q}`, { method: 'POST', credentials: 'same-origin', body: fd }), 'calendar/import');
  const data = (await r.json()) as { imported?: number; count?: number; message?: string };
  const n = Number(data.imported ?? data.count ?? 0);
  return { imported: n, message: data.message ?? `${n} evento${n === 1 ? '' : 's'} importado${n === 1 ? '' : 's'}.` };
}

export interface SyncResult {
  ok: boolean;
  pulled: number;
  pushed: number;
  errors: string[];
}

export async function syncCaldav(): Promise<SyncResult> {
  const r = await ok(await fetch('/api/calendar/sync?direction=pull', { method: 'POST', credentials: 'same-origin' }), 'calendar/sync');
  const data = (await r.json()) as Record<string, unknown>;
  const errors = Array.isArray(data.errors) ? data.errors.map((e) => (typeof e === 'string' ? e : JSON.stringify(e))) : [];
  return { ok: data.ok !== false, pulled: Number(data.pulled ?? data.imported ?? 0) || 0, pushed: Number(data.pushed ?? 0) || 0, errors };
}

export interface ParsedEvent {
  summary: string;
  dtstart: string;
  dtend: string;
  allDay: boolean;
  location: string;
  description: string;
  confidence: number;
}

/** Natural language → event, anchored on the server's clock with the browser's zone. */
export async function quickParse(text: string): Promise<ParsedEvent | null> {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const r = await ok(await fetch('/api/calendar/quick-parse', json('POST', { text, tz, tz_offset: -new Date().getTimezoneOffset() })), 'calendar/quick-parse');
  const data = (await r.json()) as { ok?: boolean; event?: Record<string, unknown>; confidence?: number };
  if (!data.ok || !data.event) return null;
  const e = data.event;
  return {
    summary: String(e.summary ?? ''),
    dtstart: String(e.dtstart ?? ''),
    dtend: String(e.dtend ?? ''),
    allDay: Boolean(e.all_day),
    location: String(e.location ?? ''),
    description: String(e.description ?? ''),
    confidence: Number(data.confidence ?? 0.7),
  };
}

/* ── Dates ── */

export function ds(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${ds(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function parseStamp(value: string): Date {
  // A bare date is a local day; anything else, the Date parser (Z = UTC).
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T00:00:00`) : new Date(value);
}

export function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

/** Monday-first week start. */
export function startOfWeek(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  const wd = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - wd);
  return out;
}

/** The days an event touches, as local YYYY-MM-DD, inside [from, to]. */
export function eventDays(ev: CalEvent): string[] {
  const start = parseStamp(ev.dtstart);
  let end = parseStamp(ev.dtend);
  if (ev.allDay) end = addDays(end, -1); // all-day dtend is exclusive
  if (end < start) end = start;
  const out: string[] = [];
  const cursor = new Date(start);
  cursor.setHours(0, 0, 0, 0);
  const last = new Date(end);
  last.setHours(0, 0, 0, 0);
  let guard = 0;
  while (cursor <= last && guard++ < 400) {
    out.push(ds(cursor));
    cursor.setDate(cursor.getDate() + 1);
  }
  return out;
}

export function fmtTime(value: string): string {
  return parseStamp(value).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
}

export function fmtDay(value: string): string {
  return parseStamp(value).toLocaleDateString(locale(), { weekday: 'long', day: 'numeric', month: 'long' });
}

/** Month names in the interface language (a function: the language can change). */
export function months(): string[] {
  const fmt = new Intl.DateTimeFormat(locale(), { month: 'long' });
  return Array.from({ length: 12 }, (_, m) => fmt.format(new Date(2024, m, 1)));
}

/** Monday-first short weekday names in the interface language. */
export function weekdays(): string[] {
  const fmt = new Intl.DateTimeFormat(locale(), { weekday: 'short' });
  // 2024-01-01 is a Monday.
  return Array.from({ length: 7 }, (_, d) => fmt.format(new Date(2024, 0, 1 + d)).replace(/\.$/, ''));
}

export const RRULES: { value: string; label: string }[] = [
  { value: '', label: 'No repeat' },
  { value: 'FREQ=DAILY', label: 'Every day' },
  { value: 'FREQ=WEEKLY', label: 'Every week' },
  { value: 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR', label: 'Weekdays' },
  { value: 'FREQ=MONTHLY', label: 'Every month' },
  { value: 'FREQ=YEARLY', label: 'Every year' },
];

export function rruleLabel(rrule: string): string {
  if (!rrule) return '';
  const head = rrule.replace(/^RRULE:/, '');
  const found = RRULES.find((r) => r.value === head);
  return found ? t(found.label) : head;
}

/** Readable text colour for a hex background. */
export function fgFor(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return 'var(--fs-text-1)';
  const n = parseInt(m[1], 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? '#1b1b1f' : '#ffffff';
}
