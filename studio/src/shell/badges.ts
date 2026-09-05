import { useEffect, useState } from 'react';

/**
 * The number on a tool in the rail.
 *
 * The previous interface put one on the calendar: how many things are on
 * today, so you can see it without opening anything. It is one small
 * request, so it is polled slowly and only while the tab is visible, and
 * the adapters are imported lazily to keep them out of the shell's chunk.
 */

export interface Badges {
  /** Events today, all-day ones included. */
  calendar: number;
  /** Notes whose reminder is today or already past. */
  notes: number;
}

const EMPTY: Badges = { calendar: 0, notes: 0 };
const EVERY_MS = 5 * 60_000;

async function read(): Promise<Badges> {
  const today = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const day = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
  const out = { ...EMPTY };
  try {
    const { listEvents, addDays, ds, eventDays } = await import('../adapters/calendar');
    const { events } = await listEvents(day, ds(addDays(today, 1)));
    out.calendar = events.filter((ev) => eventDays(ev).includes(day)).length;
  } catch {
    /* no calendar, no badge */
  }
  try {
    const { listNotes, isOverdue, isToday } = await import('../adapters/notes');
    const notes = await listNotes(false);
    out.notes = notes.filter((n) => n.dueDate && (isToday(n.dueDate) || isOverdue(n.dueDate))).length;
  } catch {
    /* no notes, no badge */
  }
  return out;
}

export function useBadges(): Badges {
  const [badges, setBadges] = useState<Badges>(EMPTY);
  useEffect(() => {
    let alive = true;
    const tick = (onlyWhenVisible = true) => {
      // The first read happens whatever the tab is doing — a badge that only
      // appears once you focus the window is a badge you never see.
      if (onlyWhenVisible && document.visibilityState !== 'visible') return;
      void read().then((next) => {
        if (alive) setBadges(next);
      });
    };
    tick(false);
    const timer = window.setInterval(() => tick(), EVERY_MS);
    const onVisible = () => tick();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);
  return badges;
}
