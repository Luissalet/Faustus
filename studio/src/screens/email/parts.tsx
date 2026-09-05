import { CalendarDays, Tag } from 'lucide-react';
import { useEffect } from 'react';
import { Link } from 'react-router';
import { t, locale } from '../../i18n';
import { tagLabel } from '../../adapters/email';
import { displayName, hueIndex, initials } from '../../lib/mail';

/** Time today, day + month this year, full date otherwise — like a phone's inbox. */
export function fmtWhen(date: string): string {
  if (!date) return '';
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return date;
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  if (d.getFullYear() === today.getFullYear()) return d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' });
  return d.toLocaleDateString(locale(), { day: 'numeric', month: 'short', year: 'numeric' });
}

export function fmtFull(date: string): string {
  if (!date) return '';
  const d = new Date(date);
  return Number.isNaN(d.getTime()) ? date : d.toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' });
}

/** Buckets for the list: today, yesterday, this week, earlier. */
export function dateBucket(date: string): string {
  const d = new Date(date);
  if (!date || Number.isNaN(d.getTime())) return t('Earlier');
  const now = new Date();
  const day = 86400000;
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d.getTime() >= startToday) return t('Today');
  if (d.getTime() >= startToday - day) return t('Yesterday');
  if (d.getTime() >= startToday - 6 * day) return t('This week');
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return t('This month');
  return t('Earlier');
}

export function Avatar({ name, email, size = 'md' }: { name: string; email: string; size?: 'sm' | 'md' | 'lg' }) {
  const label = displayName(name, email);
  return (
    <span className="fs-mail__avatar" data-hue={hueIndex(email || name)} data-size={size} aria-hidden="true">
      {initials(label)}
    </span>
  );
}

export function TagChip({ tag, calendarUid, onFilter }: { tag: string; calendarUid?: string; onFilter?: (tag: string) => void }) {
  if (tag === 'calendar') {
    if (!calendarUid) return null;
    return (
      <Link className="fs-mail__tag" data-tag="calendar" to={`/calendar?event=${encodeURIComponent(calendarUid)}`} title={t('Open the calendar event')}>
        <CalendarDays size={10} aria-hidden="true" /> {tagLabel(tag)}
      </Link>
    );
  }
  return (
    <button type="button" className="fs-mail__tag" data-tag={tag} onClick={() => onFilter?.(tag)} title={t('Show {tag} mails', { tag: tagLabel(tag) })}>
      <Tag size={10} aria-hidden="true" /> {tagLabel(tag)}
    </button>
  );
}

function typingTarget(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (el as HTMLElement).isContentEditable;
}

/**
 * Single-key shortcuts (j/k, e, #, s…) for the list and the reader. They
 * are ignored while typing or while anything sits in the overlay root.
 */
export function useMailKeys(handler: (key: string, e: KeyboardEvent) => boolean | void, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (typingTarget(document.activeElement)) return;
      if (document.getElementById('fs-overlay-root')?.childElementCount) return;
      if (handler(e.key, e)) e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [handler, enabled]);
}

export const SHORTCUTS: { keys: string; label: string }[] = [
  { keys: 'j / k', label: 'Next / previous mail' },
  { keys: 'Enter', label: 'Open' },
  { keys: 'r · a · f', label: 'Reply · reply all · forward' },
  { keys: 'e', label: 'Archive' },
  { keys: '#', label: 'Delete' },
  { keys: 's', label: 'Star' },
  { keys: 'd', label: 'Done' },
  { keys: 'u', label: 'Mark as unread' },
  { keys: 'c', label: 'Compose' },
  { keys: '/', label: 'Search' },
  { keys: 'Esc', label: 'Back to the list' },
];
