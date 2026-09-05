/**
 * The week as a timetable.
 *
 * Studio's week was seven lists of chips: it told you *what* was on
 * Wednesday but not *when*, which is the one thing a week view is for. The
 * previous interface had the hour grid, with drag to move and drag to
 * create, and a zoom for the hour height.
 *
 * This is the arithmetic of that grid — minutes to pixels, overlaps into
 * columns, snapping, and what a search matches. No DOM, no React: the
 * component (`screens/Calendar.tsx`) does the pointer work and this is what
 * the tests can hold.
 */

export interface Span {
  /** Minutes from midnight, clamped into the day. */
  from: number;
  to: number;
}

/** Hour height in pixels, three steps. The zoom, in other words. */
export const HOUR_HEIGHTS = { s: 30, m: 46, l: 72 } as const;
export type Zoom = keyof typeof HOUR_HEIGHTS;

export const DAY_MINUTES = 24 * 60;
/** What a drag lands on: quarter hours, like every calendar worth using. */
export const SNAP = 15;
/** Nothing shorter than this: a zero-length event is invisible. */
export const MIN_MINUTES = 15;

export function minutesOf(date: Date): number {
  return date.getHours() * 60 + date.getMinutes();
}

export function snap(minutes: number, step = SNAP): number {
  return Math.round(minutes / step) * step;
}

export function clampMinutes(minutes: number): number {
  return Math.min(Math.max(Math.round(minutes), 0), DAY_MINUTES);
}

export function yFromMinutes(minutes: number, hourHeight: number): number {
  return (minutes / 60) * hourHeight;
}

export function minutesFromY(y: number, hourHeight: number): number {
  return hourHeight > 0 ? (y / hourHeight) * 60 : 0;
}

/** `13:45`, without going through Intl for a number we already have. */
export function clockOf(minutes: number): string {
  const m = clampMinutes(minutes);
  const h = Math.floor(m / 60) % 24;
  return `${String(h).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

/** The part of an event that falls on one day, as minutes from its midnight. */
export function spanOn(start: Date, end: Date, dayStart: Date): Span {
  const dayEnd = new Date(dayStart);
  dayEnd.setDate(dayEnd.getDate() + 1);
  const from = start < dayStart ? 0 : minutesOf(start);
  const to = end > dayEnd ? DAY_MINUTES : minutesOf(end) || (end > dayStart ? DAY_MINUTES : 0);
  return { from: clampMinutes(from), to: clampMinutes(Math.max(to, from + MIN_MINUTES)) };
}

export interface Placed<T> {
  item: T;
  span: Span;
  /** Which of `columns` this one takes, so overlaps sit side by side. */
  column: number;
  columns: number;
}

/**
 * Overlapping events share the width.
 *
 * Sort by start, walk the day, and every time the run of overlapping events
 * ends, close the group: everyone in it gets the same column count, so two
 * meetings at ten are two halves and not one on top of the other.
 */
export function layout<T>(items: T[], spanOf: (item: T) => Span): Placed<T>[] {
  const sorted = items
    .map((item) => ({ item, span: spanOf(item) }))
    .sort((a, b) => a.span.from - b.span.from || b.span.to - a.span.to);
  const out: Placed<T>[] = [];
  let group: Placed<T>[] = [];
  let groupEnd = -1;

  const close = () => {
    const columns = group.reduce((n, p) => Math.max(n, p.column + 1), 0);
    for (const placed of group) placed.columns = columns;
    out.push(...group);
    group = [];
    groupEnd = -1;
  };

  for (const entry of sorted) {
    if (group.length && entry.span.from >= groupEnd) close();
    // The first column whose last event has already finished.
    const taken = new Set(group.filter((p) => p.span.to > entry.span.from).map((p) => p.column));
    let column = 0;
    while (taken.has(column)) column += 1;
    group.push({ item: entry.item, span: entry.span, column, columns: 1 });
    groupEnd = Math.max(groupEnd, entry.span.to);
  }
  if (group.length) close();
  return out;
}

/** A dragged event keeps its length and stays inside the day. */
export function moveTo(span: Span, newFrom: number): Span {
  const length = span.to - span.from;
  const from = Math.min(Math.max(snap(newFrom), 0), DAY_MINUTES - length);
  return { from, to: from + length };
}

/** A resized event keeps its start and never goes under the minimum. */
export function resizeTo(span: Span, newTo: number): Span {
  return { from: span.from, to: Math.min(Math.max(snap(newTo), span.from + MIN_MINUTES), DAY_MINUTES) };
}

/** A drag on empty space, in whichever direction it was drawn. */
export function spanFromDrag(a: number, b: number): Span {
  const from = Math.min(snap(a), snap(b));
  const to = Math.max(snap(a), snap(b));
  return { from: Math.max(0, from), to: Math.min(DAY_MINUTES, Math.max(to, from + MIN_MINUTES)) };
}

/** What `/calendar` searches: the words a person would remember. */
export function matches(event: { summary?: string; description?: string; location?: string; calendarName?: string }, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [event.summary, event.description, event.location, event.calendarName]
    .filter(Boolean)
    .some((field) => (field as string).toLowerCase().includes(needle));
}
