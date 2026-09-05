import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Download,
  FileUp,
  MapPin,
  Plus,
  RefreshCw,
  Repeat,
  Settings2,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Skeleton, Toast } from '../components';
import {
  addDays,
  createCalendar,
  createEvent,
  deleteCalendar,
  deleteEvent,
  ds,
  eventDays,
  exportUrl,
  fgFor,
  fmtDay,
  fmtTime,
  months,
  weekdays,
  importIcs,
  listCalendars,
  listEvents,
  parseStamp,
  quickParse,
  RRULES,
  rruleLabel,
  startOfWeek,
  syncCaldav,
  toLocalInput,
  updateCalendar,
  updateEvent,
  type CalEvent,
  type Calendar,
  type EventDraft,
} from '../adapters/calendar';
import './projects.css';
import './calendar.css';
import { t } from '../i18n';

/** The server's default calendar colour: stored per calendar, not a UI token. */
const DEFAULT_COLOR = '#5b8abf'; // guard-ok: data, not chrome

/**
 * Calendario (the previous interface's calendar modal, `/calendar`).
 *
 * Month, week, agenda and year over `/api/calendar`; events by day with the
 * calendar's colour; a form for create/edit/delete (with the recurring
 * scope), quick add in plain words (`/quick-parse`), filters per calendar,
 * calendars (new, rename, colour, delete, export .ics, import .ics) and
 * CalDAV sync. The CalDAV account form itself stays in the previous
 * interface's settings.
 */

type View = 'month' | 'week' | 'agenda' | 'year';
const VIEW_KEY = 'faustus_studio_calendar_view';

function readView(): View {
  try {
    const v = localStorage.getItem(VIEW_KEY);
    return v === 'week' || v === 'agenda' || v === 'year' ? v : 'month';
  } catch {
    return 'month';
  }
}

function monthGrid(d: Date): Date[] {
  const first = new Date(d.getFullYear(), d.getMonth(), 1);
  const start = startOfWeek(first);
  const out: Date[] = [];
  for (let i = 0; i < 42; i++) out.push(addDays(start, i));
  return out;
}

function rangeFor(view: View, d: Date): { start: string; end: string } {
  if (view === 'month') {
    const grid = monthGrid(d);
    return { start: ds(grid[0]), end: ds(addDays(grid[41], 1)) };
  }
  if (view === 'week') {
    const s = startOfWeek(d);
    return { start: ds(s), end: ds(addDays(s, 7)) };
  }
  if (view === 'agenda') {
    const s = new Date(d);
    s.setHours(0, 0, 0, 0);
    return { start: ds(s), end: ds(addDays(s, 60)) };
  }
  return { start: `${d.getFullYear()}-01-01`, end: `${d.getFullYear() + 1}-01-01` };
}

function step(view: View, d: Date, dir: -1 | 1): Date {
  const out = new Date(d);
  if (view === 'month') out.setMonth(out.getMonth() + dir);
  else if (view === 'week') out.setDate(out.getDate() + 7 * dir);
  else if (view === 'agenda') out.setDate(out.getDate() + 30 * dir);
  else out.setFullYear(out.getFullYear() + dir);
  return out;
}

function isoWeek(d: Date): number {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t.getTime() - y0.getTime()) / 86400000 + 1) / 7);
}

/* ── Event form ── */

interface FormState {
  summary: string;
  allDay: boolean;
  start: string; // datetime-local or date
  end: string;
  location: string;
  description: string;
  calendarId: string;
  rrule: string;
  color: string;
}

function formFrom(ev: CalEvent | null, day: string | null, calendars: Calendar[], title = ''): FormState {
  if (ev) {
    const s = parseStamp(ev.dtstart);
    const e = parseStamp(ev.dtend);
    return {
      summary: ev.summary,
      allDay: ev.allDay,
      start: ev.allDay ? ds(s) : toLocalInput(s),
      end: ev.allDay ? ds(addDays(e, -1)) : toLocalInput(e),
      location: ev.location,
      description: ev.description,
      calendarId: ev.calendarId,
      rrule: ev.rrule.replace(/^RRULE:/, ''),
      color: ev.color && ev.color !== calendars.find((c) => c.id === ev.calendarId)?.color ? ev.color : '',
    };
  }
  const base = day ? new Date(`${day}T09:00:00`) : new Date();
  if (!day) base.setMinutes(0, 0, 0), base.setHours(base.getHours() + 1);
  const end = new Date(base);
  end.setHours(end.getHours() + 1);
  return { summary: title, allDay: false, start: toLocalInput(base), end: toLocalInput(end), location: '', description: '', calendarId: calendars[0]?.id ?? '', rrule: '', color: '' };
}

function draftFrom(f: FormState): EventDraft {
  return {
    summary: f.summary.trim(),
    allDay: f.allDay,
    dtstart: f.allDay ? f.start.slice(0, 10) : f.start,
    dtend: f.allDay ? ds(addDays(parseStamp(f.end.slice(0, 10)), 1)) : f.end,
    location: f.location.trim(),
    description: f.description.trim(),
    calendarId: f.calendarId || null,
    rrule: f.rrule || null,
    color: f.color || null,
  };
}

function EventDialog({ event, day, calendars, title, onClose, onSaved, onDeleted, say }: { event: CalEvent | null; day: string | null; calendars: Calendar[]; title?: string; onClose: () => void; onSaved: () => void; onDeleted: () => void; say: (t: string) => void }) {
  const [f, setF] = useState<FormState>(() => formFrom(event, day, calendars, title));
  const [saving, setSaving] = useState(false);
  const [askScope, setAskScope] = useState(false);
  const set = (p: Partial<FormState>) => setF((s) => ({ ...s, ...p }));
  const recurringOccurrence = Boolean(event && event.rrule && event.uid.includes('::'));

  const toggleAllDay = (allDay: boolean) => {
    if (allDay) set({ allDay, start: f.start.slice(0, 10), end: f.end.slice(0, 10) });
    else {
      const s = f.start.length === 10 ? `${f.start}T09:00` : f.start;
      const e = f.end.length === 10 ? `${f.end}T10:00` : f.end;
      set({ allDay, start: s, end: e });
    }
  };

  const save = async () => {
    if (!f.summary.trim()) {
      say(t('Give it a title.'));
      return;
    }
    setSaving(true);
    try {
      const draft = draftFrom(f);
      if (event) await updateEvent(event.uid, draft);
      else await createEvent(draft);
      onSaved();
    } catch (err) {
      say((err as Error).message || t('Could not save the event.'));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (scope: 'series' | 'occurrence') => {
    if (!event) return;
    setSaving(true);
    try {
      await deleteEvent(event.uid, scope);
      onDeleted();
    } catch (err) {
      say((err as Error).message || t('Could not delete the event.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={event ? t('Edit the event') : t('New event')}
      testId="event-dialog"
      footer={
        <div className="fs-cal-form__foot">
          {event && !askScope && <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={() => (recurringOccurrence ? setAskScope(true) : void remove('series'))} />}
          {event && askScope && (
            <>
              <Button variant="danger" size="sm" label={t('Just this one')} onClick={() => void remove('occurrence')} />
              <Button variant="danger-solid" size="sm" label={t('The whole series')} onClick={() => void remove('series')} />
            </>
          )}
          <span className="fs-cal-form__spacer" />
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
          <Button variant="primary" size="sm" label={event ? t('Save') : t('Create')} loading={saving} onClick={() => void save()} testId="event-save" />
        </div>
      }
    >
      <div className="fs-cal-form">
        <input type="text" className="fs-cal-form__title" placeholder={t('Title')} value={f.summary} onChange={(e) => set({ summary: e.target.value })} autoFocus={!event} />
        <label className="fs-switch">
          <input type="checkbox" checked={f.allDay} onChange={(e) => toggleAllDay(e.target.checked)} /> <span>{t('All day')}</span>
        </label>
        <div className="fs-cal-form__row">
          <span className="fs-cal-form__label">Empieza</span>
          <input type={f.allDay ? 'date' : 'datetime-local'} className="fs-field" value={f.start} onChange={(e) => set({ start: e.target.value })} />
        </div>
        <div className="fs-cal-form__row">
          <span className="fs-cal-form__label">Termina</span>
          <input type={f.allDay ? 'date' : 'datetime-local'} className="fs-field" value={f.end} onChange={(e) => set({ end: e.target.value })} />
        </div>
        <div className="fs-cal-form__row">
          <span className="fs-cal-form__label">
            <Repeat size={12} aria-hidden="true" /> Repetir
          </span>
          <select className="fs-field" value={RRULES.some((r) => r.value === f.rrule) ? f.rrule : 'custom'} onChange={(e) => set({ rrule: e.target.value === 'custom' ? f.rrule : e.target.value })}>
            {RRULES.map((r) => (
              <option key={r.value} value={r.value}>
                {t(r.label)}
              </option>
            ))}
            {!RRULES.some((r) => r.value === f.rrule) && <option value="custom">{f.rrule}</option>}
          </select>
        </div>
        <div className="fs-cal-form__row">
          <span className="fs-cal-form__label">
            <MapPin size={12} aria-hidden="true" /> Lugar
          </span>
          <input type="text" className="fs-field" value={f.location} onChange={(e) => set({ location: e.target.value })} />
        </div>
        <div className="fs-cal-form__row">
          <span className="fs-cal-form__label">Calendario</span>
          <select className="fs-field" value={f.calendarId} onChange={(e) => set({ calendarId: e.target.value })} disabled={Boolean(event)}>
            {calendars.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
        <div className="fs-cal-form__row">
          <span className="fs-cal-form__label">{t('Colour')}</span>
          <div className="fs-cal-form__color">
            <input type="color" value={f.color || calendars.find((c) => c.id === f.calendarId)?.color || DEFAULT_COLOR} onChange={(e) => set({ color: e.target.value })} aria-label={t('Event colour')} />
            {f.color && (
              <button type="button" className="fs-chip" onClick={() => set({ color: '' })}>
                <X size={12} aria-hidden="true" /> El del calendario
              </button>
            )}
          </div>
        </div>
        <textarea className="fs-cal-form__desc" placeholder={t('Notes')} rows={3} value={f.description} onChange={(e) => set({ description: e.target.value })} />
      </div>
    </Dialog>
  );
}

/* ── Calendars dialog ── */

function CalendarsDialog({ calendars, onClose, onChanged, say }: { calendars: Calendar[]; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const [rows, setRows] = useState(calendars.map((c) => ({ ...c })));
  const [busy, setBusy] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [importTo, setImportTo] = useState('');

  useEffect(() => setRows(calendars.map((c) => ({ ...c }))), [calendars]);

  const saveRow = async (row: Calendar) => {
    setBusy(row.id);
    try {
      await updateCalendar(row.id, row.name, row.color);
      onChanged();
    } catch {
      say(t('Could not save the calendar.'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={t('Calendars')}
      description={t('Each one\'s name and colour; export or import .ics. CalDAV accounts are set up in the previous interface\'s settings.')}
      footer={<Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />}
    >
      <div className="fs-cal-cals">
        {rows.map((row) => (
          <div key={row.id} className="fs-cal-cals__row">
            <input type="color" value={row.color} onChange={(e) => setRows((r) => r.map((x) => (x.id === row.id ? { ...x, color: e.target.value } : x)))} onBlur={() => void saveRow(row)} aria-label={t('Colour')} />
            <input type="text" className="fs-field" value={row.name} onChange={(e) => setRows((r) => r.map((x) => (x.id === row.id ? { ...x, name: e.target.value } : x)))} onBlur={() => void saveRow(row)} aria-label={t('Name')} />
            <span className="fs-cal-cals__source">{row.source === 'caldav' ? 'CalDAV' : t('local')}</span>
            <a className="fs-btn" data-size="sm" data-variant="ghost" href={exportUrl(row.id)} download title={t('Export .ics')}>
              <Download size={13} aria-hidden="true" />
            </a>
            <IconButton
              icon={Trash2}
              label={t('Delete the calendar and its events')}
              size="sm"
              disabled={busy === row.id || rows.length <= 1}
              onClick={() => {
                if (!window.confirm(t('Delete "{name}" with all its events?', { name: row.name }))) return;
                setBusy(row.id);
                void deleteCalendar(row.id)
                  .then(onChanged)
                  .catch(() => say(t('Could not delete the calendar.')))
                  .finally(() => setBusy(null));
              }}
            />
          </div>
        ))}
        <div className="fs-cal-cals__tools">
          <Button
            variant="secondary"
            size="sm"
            icon={Plus}
            label={t('New calendar')}
            loading={busy === 'new'}
            onClick={() => {
              setBusy('new');
              void createCalendar(t('New calendar'), DEFAULT_COLOR)
                .then(onChanged)
                .catch(() => say(t('Could not create the calendar.')))
                .finally(() => setBusy(null));
            }}
          />
          <select className="fs-field" value={importTo} onChange={(e) => setImportTo(e.target.value)} aria-label={t('Import into')}>
            <option value="">{t('Import into: the file\'s')}</option>
            {rows.map((r) => (
              <option key={r.id} value={r.name}>
                {t('Import into')}: {r.name}
              </option>
            ))}
          </select>
          <Button variant="secondary" size="sm" icon={FileUp} label={t('Import .ics')} loading={busy === 'import'} onClick={() => fileRef.current?.click()} />
          <input
            ref={fileRef}
            type="file"
            accept=".ics,text/calendar"
            hidden
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              setBusy('import');
              void importIcs(file, importTo)
                .then((r) => {
                  say(r.message);
                  onChanged();
                })
                .catch((err: Error) => say(err.message || t('Could not import the file.')))
                .finally(() => {
                  setBusy(null);
                  if (fileRef.current) fileRef.current.value = '';
                });
            }}
          />
        </div>
      </div>
    </Dialog>
  );
}

/* ── Event chip (month / week / year) ── */

function EventChip({ ev, onOpen, compact }: { ev: CalEvent; onOpen: () => void; compact?: boolean }) {
  const bg = ev.color || DEFAULT_COLOR;
  return (
    <button type="button" className="fs-cal-ev" style={{ background: bg, color: fgFor(bg) }} onClick={onOpen} title={ev.summary} data-testid="cal-event">
      {!ev.allDay && !compact && <span className="fs-cal-ev__time">{fmtTime(ev.dtstart)}</span>}
      <span className="fs-cal-ev__title">{ev.summary || t('(untitled)')}</span>
      {ev.rrule && <Repeat size={10} aria-label={t('Repeats')} />}
    </button>
  );
}

/* ── Screen ── */

export function CalendarScreen() {
  const [params, setParams] = useSearchParams();
  const [view, setView] = useState<View>(readView);
  const [cursor, setCursor] = useState<Date>(() => {
    const p = params.get('d');
    return p ? parseStamp(p) : new Date();
  });
  const [calendars, setCalendars] = useState<Calendar[] | null>(null);
  const [events, setEvents] = useState<CalEvent[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [failed, setFailed] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [selectedDay, setSelectedDay] = useState<string>(() => ds(new Date()));
  const [dialog, setDialog] = useState<{ event: CalEvent | null; day: string | null } | null>(null);
  const [calsOpen, setCalsOpen] = useState(false);
  const [prefill, setPrefill] = useState('');
  const [quick, setQuick] = useState('');
  const [parsing, setParsing] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const today = ds(new Date());

  const say = useCallback((t: string) => {
    setNotice(t);
    window.setTimeout(() => setNotice((c) => (c === t ? null : c)), 4000);
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch {
      /* ignore */
    }
  }, [view]);

  useEffect(() => {
    if (params.get('d')) {
      const next = new URLSearchParams(params);
      next.delete('d');
      setParams(next, { replace: true });
    }
  }, [params, setParams]);

  useEffect(() => {
    const c = new AbortController();
    listCalendars(c.signal)
      .then(setCalendars)
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== 'AbortError') setFailed(true);
      });
    return () => c.abort();
  }, [reload]);

  const range = useMemo(() => rangeFor(view, cursor), [view, cursor]);
  useEffect(() => {
    const c = new AbortController();
    listEvents(range.start, range.end, c.signal)
      .then((r) => {
        setEvents(r.events);
        setTruncated(r.truncated);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== 'AbortError') setFailed(true);
      });
    return () => c.abort();
  }, [range.start, range.end, reload]);

  const refresh = () => setReload((n) => n + 1);

  /* ?event=<uid> (a mail's calendar tag): find it in the next two years,
     jump to its day and open it. */
  useEffect(() => {
    const wanted = params.get('event');
    if (!wanted) return;
    const c = new AbortController();
    const now = new Date();
    listEvents(ds(new Date(now.getFullYear(), 0, 1)), ds(new Date(now.getFullYear() + 2, 0, 1)), c.signal)
      .then((r) => {
        const ev = r.events.find((e) => e.uid === wanted || e.uid.startsWith(wanted));
        if (ev) {
          const day = eventDays(ev)[0];
          if (day) {
            setCursor(parseStamp(day));
            setSelectedDay(day);
          }
          setDialog({ event: ev, day: null });
        } else say(t('That calendar event is no longer there.'));
        const next = new URLSearchParams(params);
        next.delete('event');
        setParams(next, { replace: true });
      })
      .catch(() => undefined);
    return () => c.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.get('event')]);

  const byDay = useMemo(() => {
    const map = new Map<string, CalEvent[]>();
    for (const ev of events ?? []) {
      if (hidden.has(ev.calendarId)) continue;
      for (const day of eventDays(ev)) {
        const list = map.get(day) ?? [];
        list.push(ev);
        map.set(day, list);
      }
    }
    for (const list of map.values()) list.sort((a, b) => Number(b.allDay) - Number(a.allDay) || a.dtstart.localeCompare(b.dtstart));
    return map;
  }, [events, hidden]);

  const goToday = () => {
    setCursor(new Date());
    setSelectedDay(today);
  };

  /* Keyboard: arrows move, T today, N new, M/W/A/Y views. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      if (dialog || calsOpen) return;
      if (e.key === 'ArrowLeft') setCursor((d) => step(view, d, -1));
      else if (e.key === 'ArrowRight') setCursor((d) => step(view, d, 1));
      else if (e.key === 't' || e.key === 'T') goToday();
      else if (e.key === 'n' || e.key === 'N') setDialog({ event: null, day: selectedDay });
      else if (e.key === 'm') setView('month');
      else if (e.key === 'w') setView('week');
      else if (e.key === 'a') setView('agenda');
      else if (e.key === 'y') setView('year');
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, dialog, calsOpen, selectedDay]);

  const quickAdd = async () => {
    const text = quick.trim();
    if (!text) return;
    setParsing(true);
    try {
      const parsed = await quickParse(text);
      if (!parsed || !parsed.dtstart) {
        say(t('I did not understand it: open the form.'));
        setPrefill(text);
        setDialog({ event: null, day: selectedDay });
        return;
      }
      await createEvent({
        summary: parsed.summary || text,
        dtstart: parsed.allDay ? parsed.dtstart.slice(0, 10) : parsed.dtstart,
        dtend: parsed.allDay ? ds(addDays(parseStamp(parsed.dtend.slice(0, 10) || parsed.dtstart.slice(0, 10)), 1)) : parsed.dtend || null,
        allDay: parsed.allDay,
        location: parsed.location,
        description: parsed.description,
      });
      setQuick('');
      setCursor(parseStamp(parsed.dtstart));
      setSelectedDay(parsed.dtstart.slice(0, 10));
      say(`${t('Created')}: ${parsed.summary || text}${parsed.confidence < 0.6 ? t(' (check it)') : ''}`);
      refresh();
    } catch (err) {
      // The model did not answer (or answered nonsense): the form, with the
      // words as the title, is one click from done.
      say(`${(err as Error).message || t('Could not interpret it')}: ${t('open the form.')}`);
      setPrefill(text);
      setDialog({ event: null, day: selectedDay });
    } finally {
      setParsing(false);
    }
  };

  const sync = async () => {
    setSyncing(true);
    try {
      const r = await syncCaldav();
      say(r.errors.length ? `${t('Synced with warnings')}: ${r.errors[0]}` : t('Synced: {a} pulled, {b} pushed.', { a: r.pulled, b: r.pushed }));
      refresh();
    } catch (err) {
      say((err as Error).message || t('Could not sync.'));
    } finally {
      setSyncing(false);
    }
  };

  const title = useMemo(() => {
    if (view === 'agenda') return t('Next 60 days');
    if (view === 'year') return String(cursor.getFullYear());
    if (view === 'week') {
      const s = startOfWeek(cursor);
      const e = addDays(s, 6);
      const M = months();
      return `${s.getDate()} ${M[s.getMonth()].slice(0, 3)} – ${e.getDate()} ${M[e.getMonth()].slice(0, 3)} · ${t('week')} ${isoWeek(s)}`;
    }
    return `${months()[cursor.getMonth()]} ${cursor.getFullYear()}`;
  }, [view, cursor]);

  if (failed) {
    return (
      <EmptyState
        icon={CalendarDays}
        title={t('Could not read the calendar')}
        body={t('The calendar endpoint is not responding. The previous interface does not depend on this screen.')}
        primaryAction={{
          label: t('Open the previous interface'),
          onClick: () => {
            window.location.href = '/calendar?shell=legacy';
          },
        }}
      />
    );
  }

  const hasCaldav = (calendars ?? []).some((c) => c.source === 'caldav');
  const dayEvents = byDay.get(selectedDay) ?? [];

  return (
    <div className="fs-screen fs-cal" data-testid="calendar" data-view={view}>
      <header className="fs-screen__head fs-cal__head">
        <div>
          <h1 className="fs-screen__title">Calendario</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            Escribe abajo en tus palabras («comida con Marta el viernes a las 2») o pulsa N. Flechas para moverte, T para hoy.
          </p>
        </div>
        <div className="fs-cal__tools">
          {hasCaldav && <IconButton icon={RefreshCw} label={t('Sync with CalDAV')} size="sm" onClick={() => void sync()} disabled={syncing} />}
          <IconButton icon={Settings2} label={t('Calendars')} size="sm" onClick={() => setCalsOpen(true)} />
          <Button variant="primary" size="sm" icon={Plus} label={t('New')} onClick={() => setDialog({ event: null, day: selectedDay })} testId="cal-new" />
        </div>
      </header>

      <form
        className="fs-cal__quick"
        onSubmit={(e) => {
          e.preventDefault();
          void quickAdd();
        }}
      >
        <Sparkles size={14} aria-hidden="true" className="fs-cal__quick-icon" />
        <input type="text" className="fs-cal__quick-input" placeholder={t('Add an event in your own words…')} value={quick} onChange={(e) => setQuick(e.target.value)} data-testid="cal-quick" />
        <Button type="submit" variant="secondary" size="sm" label={t('Add')} disabled={!quick.trim()} loading={parsing} />
      </form>

      <div className="fs-cal__bar">
        <div className="fs-cal__nav">
          <IconButton icon={ChevronLeft} label={t('Previous')} size="sm" onClick={() => setCursor((d) => step(view, d, -1))} />
          <button type="button" className="fs-chip" onClick={goToday}>
            Hoy
          </button>
          <IconButton icon={ChevronRight} label={t('Next')} size="sm" onClick={() => setCursor((d) => step(view, d, 1))} />
          <h2 className="fs-cal__title">{title}</h2>
        </div>
        <div className="fs-cal__views" role="group" aria-label={t('View')}>
          {(['month', 'week', 'agenda', 'year'] as View[]).map((v) => (
            <button key={v} type="button" className="fs-chip" data-on={view === v || undefined} onClick={() => setView(v)}>
              {v === 'month' ? t('Month') : v === 'week' ? t('Week') : v === 'agenda' ? t('Agenda') : t('Year')}
            </button>
          ))}
        </div>
      </div>

      {calendars && calendars.length > 1 && (
        <div className="fs-cal__filters" role="group" aria-label={t('Visible calendars')}>
          {calendars.map((c) => (
            <button
              key={c.id}
              type="button"
              className="fs-chip fs-cal__filter"
              data-on={!hidden.has(c.id) || undefined}
              onClick={() =>
                setHidden((cur) => {
                  const next = new Set(cur);
                  if (next.has(c.id)) next.delete(c.id);
                  else next.add(c.id);
                  return next;
                })
              }
            >
              <span className="fs-cal__dot" style={{ background: c.color }} aria-hidden="true" />
              {c.name}
            </button>
          ))}
        </div>
      )}

      {truncated && <p className="fs-cal__warn">{t('There are more repetitions than the server expands; narrow the range.')}</p>}

      {(!events || !calendars) && <Skeleton label={t('Loading the calendar')} count={5} height="64px" />}

      {events && calendars && view === 'month' && (
        <div className="fs-cal__month">
          <div className="fs-cal__weekdays">
            {weekdays().map((w) => (
              <span key={w}>{w}</span>
            ))}
          </div>
          <div className="fs-cal__grid">
            {monthGrid(cursor).map((d) => {
              const key = ds(d);
              const list = byDay.get(key) ?? [];
              return (
                <div
                  key={key}
                  className="fs-cal__cell"
                  data-other={d.getMonth() !== cursor.getMonth() || undefined}
                  data-today={key === today || undefined}
                  data-selected={key === selectedDay || undefined}
                  onClick={() => setSelectedDay(key)}
                  onDoubleClick={() => setDialog({ event: null, day: key })}
                  role="button"
                  tabIndex={-1}
                >
                  <span className="fs-cal__daynum">{d.getDate()}</span>
                  <div className="fs-cal__cell-events">
                    {list.slice(0, 3).map((ev) => (
                      <EventChip key={ev.uid} ev={ev} compact onOpen={() => setDialog({ event: ev, day: null })} />
                    ))}
                    {list.length > 3 && <span className="fs-cal__more">+{list.length - 3}</span>}
                  </div>
                </div>
              );
            })}
          </div>
          <aside className="fs-cal__day" aria-label={t('Selected day')}>
            <header className="fs-cal__day-head">
              <span>{fmtDay(selectedDay)}</span>
              <Button variant="ghost" size="sm" icon={Plus} label={t('Event')} onClick={() => setDialog({ event: null, day: selectedDay })} />
            </header>
            {dayEvents.length === 0 && <p className="fs-cal__empty">{t('Nothing that day.')}</p>}
            {dayEvents.map((ev) => (
              <button key={ev.uid} type="button" className="fs-cal__day-ev" onClick={() => setDialog({ event: ev, day: null })}>
                <span className="fs-cal__dot" style={{ background: ev.color || DEFAULT_COLOR }} aria-hidden="true" />
                <span className="fs-cal__day-when">{ev.allDay ? t('all day') : `${fmtTime(ev.dtstart)}–${fmtTime(ev.dtend)}`}</span>
                <span className="fs-cal__day-title">{ev.summary || t('(untitled)')}</span>
                {ev.location && (
                  <span className="fs-cal__day-loc">
                    <MapPin size={10} aria-hidden="true" /> {ev.location}
                  </span>
                )}
                {ev.rrule && <span className="fs-cal__day-rep">{rruleLabel(ev.rrule)}</span>}
              </button>
            ))}
          </aside>
        </div>
      )}

      {events && calendars && view === 'week' && (
        <div className="fs-cal__week">
          {Array.from({ length: 7 }, (_, i) => addDays(startOfWeek(cursor), i)).map((d, i) => {
            const key = ds(d);
            const list = byDay.get(key) ?? [];
            return (
              <section key={key} className="fs-cal__wday" data-today={key === today || undefined}>
                <header className="fs-cal__wday-head" onDoubleClick={() => setDialog({ event: null, day: key })}>
                  <span className="fs-cal__wday-name">{weekdays()[i]}</span>
                  <span className="fs-cal__wday-num">{d.getDate()}</span>
                </header>
                <div className="fs-cal__wday-list">
                  {list.map((ev) => (
                    <EventChip key={ev.uid} ev={ev} onOpen={() => setDialog({ event: ev, day: null })} />
                  ))}
                  <button type="button" className="fs-cal__wday-add" onClick={() => setDialog({ event: null, day: key })} aria-label={t('New event on {day}', { day: key })}>
                    <Plus size={12} aria-hidden="true" />
                  </button>
                </div>
              </section>
            );
          })}
        </div>
      )}

      {events && calendars && view === 'agenda' && (
        <div className="fs-cal__agenda">
          {[...byDay.keys()]
            .filter((k) => k >= range.start && k < range.end)
            .sort()
            .map((key) => (
              <section key={key} className="fs-cal__aday" data-today={key === today || undefined}>
                <h3 className="fs-cal__aday-head">{fmtDay(key)}</h3>
                {(byDay.get(key) ?? []).map((ev) => (
                  <button key={ev.uid} type="button" className="fs-cal__day-ev" onClick={() => setDialog({ event: ev, day: null })}>
                    <span className="fs-cal__dot" style={{ background: ev.color || DEFAULT_COLOR }} aria-hidden="true" />
                    <span className="fs-cal__day-when">{ev.allDay ? t('all day') : `${fmtTime(ev.dtstart)}–${fmtTime(ev.dtend)}`}</span>
                    <span className="fs-cal__day-title">{ev.summary || t('(untitled)')}</span>
                    {ev.location && (
                      <span className="fs-cal__day-loc">
                        <MapPin size={10} aria-hidden="true" /> {ev.location}
                      </span>
                    )}
                  </button>
                ))}
              </section>
            ))}
          {byDay.size === 0 && <EmptyState icon={CalendarDays} title={t('Nothing in the next 60 days')} body={t('Add something above in your own words or with N.')} />}
        </div>
      )}

      {events && calendars && view === 'year' && (
        <div className="fs-cal__year">
          {months().map((name, m) => {
            const first = new Date(cursor.getFullYear(), m, 1);
            const grid = monthGrid(first);
            return (
              <section key={name} className="fs-cal__ymonth">
                <button
                  type="button"
                  className="fs-cal__ymonth-head"
                  onClick={() => {
                    setCursor(first);
                    setView('month');
                  }}
                >
                  {name}
                </button>
                <div className="fs-cal__ygrid">
                  {grid.map((d) => {
                    const key = ds(d);
                    const n = d.getMonth() === m ? (byDay.get(key)?.length ?? 0) : 0;
                    return (
                      <span
                        key={key}
                        className="fs-cal__yday"
                        data-other={d.getMonth() !== m || undefined}
                        data-today={key === today || undefined}
                        data-busy={n > 0 ? Math.min(n, 3) : undefined}
                        title={n ? `${n} evento${n === 1 ? '' : 's'}` : undefined}
                        onClick={() => {
                          if (d.getMonth() !== m) return;
                          setCursor(d);
                          setSelectedDay(key);
                          setView('month');
                        }}
                      >
                        {d.getMonth() === m ? d.getDate() : ''}
                      </span>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </div>
      )}

      {dialog && calendars && (
        <EventDialog
          event={dialog.event}
          day={dialog.day}
          calendars={calendars}
          title={prefill}
          onClose={() => {
            setDialog(null);
            setPrefill('');
          }}
          onSaved={() => {
            setDialog(null);
            setPrefill('');
            setQuick('');
            say(t('Saved.'));
            refresh();
          }}
          onDeleted={() => {
            setDialog(null);
            say(t('Deleted.'));
            refresh();
          }}
          say={say}
        />
      )}

      {calsOpen && calendars && <CalendarsDialog calendars={calendars} onClose={() => setCalsOpen(false)} onChanged={refresh} say={say} />}

      {notice && (
        <Toast>{notice}</Toast>
      )}
    </div>
  );
}
