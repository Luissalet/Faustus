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
  importIcs,
  listCalendars,
  listEvents,
  MONTHS,
  parseStamp,
  quickParse,
  RRULES,
  rruleLabel,
  startOfWeek,
  syncCaldav,
  toLocalInput,
  updateCalendar,
  updateEvent,
  WEEKDAYS,
  type CalEvent,
  type Calendar,
  type EventDraft,
} from '../adapters/calendar';
import './projects.css';
import './calendar.css';

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
      say('Ponle un título.');
      return;
    }
    setSaving(true);
    try {
      const draft = draftFrom(f);
      if (event) await updateEvent(event.uid, draft);
      else await createEvent(draft);
      onSaved();
    } catch (err) {
      say((err as Error).message || 'No he podido guardar el evento.');
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
      say((err as Error).message || 'No he podido borrar el evento.');
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
      title={event ? 'Editar el evento' : 'Nuevo evento'}
      testId="event-dialog"
      footer={
        <div className="fs-cal-form__foot">
          {event && !askScope && <Button variant="danger" size="sm" icon={Trash2} label="Borrar" onClick={() => (recurringOccurrence ? setAskScope(true) : void remove('series'))} />}
          {event && askScope && (
            <>
              <Button variant="danger" size="sm" label="Solo esta vez" onClick={() => void remove('occurrence')} />
              <Button variant="danger-solid" size="sm" label="Toda la serie" onClick={() => void remove('series')} />
            </>
          )}
          <span className="fs-cal-form__spacer" />
          <Button variant="ghost" size="sm" label="Cancelar" onClick={onClose} />
          <Button variant="primary" size="sm" label={event ? 'Guardar' : 'Crear'} loading={saving} onClick={() => void save()} testId="event-save" />
        </div>
      }
    >
      <div className="fs-cal-form">
        <input type="text" className="fs-cal-form__title" placeholder="Título" value={f.summary} onChange={(e) => set({ summary: e.target.value })} autoFocus={!event} />
        <label className="fs-switch">
          <input type="checkbox" checked={f.allDay} onChange={(e) => toggleAllDay(e.target.checked)} /> <span>Todo el día</span>
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
                {r.label}
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
          <span className="fs-cal-form__label">Color</span>
          <div className="fs-cal-form__color">
            <input type="color" value={f.color || calendars.find((c) => c.id === f.calendarId)?.color || '#5b8abf'} onChange={(e) => set({ color: e.target.value })} aria-label="Color del evento" />
            {f.color && (
              <button type="button" className="fs-chip" onClick={() => set({ color: '' })}>
                <X size={12} aria-hidden="true" /> El del calendario
              </button>
            )}
          </div>
        </div>
        <textarea className="fs-cal-form__desc" placeholder="Notas" rows={3} value={f.description} onChange={(e) => set({ description: e.target.value })} />
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
      say('No he podido guardar el calendario.');
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
      title="Calendarios"
      description="Nombre y color de cada uno; exportar o importar .ics. Las cuentas CalDAV se configuran en los ajustes de la interfaz anterior."
      footer={<Button variant="ghost" size="sm" label="Cerrar" onClick={onClose} />}
    >
      <div className="fs-cal-cals">
        {rows.map((row) => (
          <div key={row.id} className="fs-cal-cals__row">
            <input type="color" value={row.color} onChange={(e) => setRows((r) => r.map((x) => (x.id === row.id ? { ...x, color: e.target.value } : x)))} onBlur={() => void saveRow(row)} aria-label="Color" />
            <input type="text" className="fs-field" value={row.name} onChange={(e) => setRows((r) => r.map((x) => (x.id === row.id ? { ...x, name: e.target.value } : x)))} onBlur={() => void saveRow(row)} aria-label="Nombre" />
            <span className="fs-cal-cals__source">{row.source === 'caldav' ? 'CalDAV' : 'local'}</span>
            <a className="fs-btn" data-size="sm" data-variant="ghost" href={exportUrl(row.id)} download title="Exportar .ics">
              <Download size={13} aria-hidden="true" />
            </a>
            <IconButton
              icon={Trash2}
              label="Borrar el calendario y sus eventos"
              size="sm"
              disabled={busy === row.id || rows.length <= 1}
              onClick={() => {
                if (!window.confirm(`¿Borrar «${row.name}» con todos sus eventos?`)) return;
                setBusy(row.id);
                void deleteCalendar(row.id)
                  .then(onChanged)
                  .catch(() => say('No he podido borrar el calendario.'))
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
            label="Nuevo calendario"
            loading={busy === 'new'}
            onClick={() => {
              setBusy('new');
              void createCalendar('Nuevo calendario', '#5b8abf')
                .then(onChanged)
                .catch(() => say('No he podido crear el calendario.'))
                .finally(() => setBusy(null));
            }}
          />
          <select className="fs-field" value={importTo} onChange={(e) => setImportTo(e.target.value)} aria-label="Importar en">
            <option value="">Importar en: el del fichero</option>
            {rows.map((r) => (
              <option key={r.id} value={r.name}>
                Importar en: {r.name}
              </option>
            ))}
          </select>
          <Button variant="secondary" size="sm" icon={FileUp} label="Importar .ics" loading={busy === 'import'} onClick={() => fileRef.current?.click()} />
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
                .catch((err: Error) => say(err.message || 'No he podido importar el fichero.'))
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
  const bg = ev.color || '#5b8abf';
  return (
    <button type="button" className="fs-cal-ev" style={{ background: bg, color: fgFor(bg) }} onClick={onOpen} title={ev.summary} data-testid="cal-event">
      {!ev.allDay && !compact && <span className="fs-cal-ev__time">{fmtTime(ev.dtstart)}</span>}
      <span className="fs-cal-ev__title">{ev.summary || '(sin título)'}</span>
      {ev.rrule && <Repeat size={10} aria-label="Se repite" />}
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
        say('No lo he entendido: abre el formulario.');
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
      say(`Creado: ${parsed.summary || text}${parsed.confidence < 0.6 ? ' (revísalo)' : ''}`);
      refresh();
    } catch (err) {
      // The model did not answer (or answered nonsense): the form, with the
      // words as the title, is one click from done.
      say(`${(err as Error).message || 'No he podido interpretarlo'}: abre el formulario.`);
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
      say(r.errors.length ? `Sincronizado con avisos: ${r.errors[0]}` : `Sincronizado: ${r.pulled} traídos, ${r.pushed} enviados.`);
      refresh();
    } catch (err) {
      say((err as Error).message || 'No he podido sincronizar.');
    } finally {
      setSyncing(false);
    }
  };

  const title = useMemo(() => {
    if (view === 'agenda') return 'Próximos 60 días';
    if (view === 'year') return String(cursor.getFullYear());
    if (view === 'week') {
      const s = startOfWeek(cursor);
      const e = addDays(s, 6);
      return `${s.getDate()} ${MONTHS[s.getMonth()].slice(0, 3)} – ${e.getDate()} ${MONTHS[e.getMonth()].slice(0, 3)} · semana ${isoWeek(s)}`;
    }
    return `${MONTHS[cursor.getMonth()]} ${cursor.getFullYear()}`;
  }, [view, cursor]);

  if (failed) {
    return (
      <EmptyState
        icon={CalendarDays}
        title="No he podido leer el calendario"
        body="El endpoint de calendario no responde. La interfaz anterior no depende de esta pantalla."
        primaryAction={{
          label: 'Abrir la interfaz anterior',
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
          {hasCaldav && <IconButton icon={RefreshCw} label="Sincronizar con CalDAV" size="sm" onClick={() => void sync()} disabled={syncing} />}
          <IconButton icon={Settings2} label="Calendarios" size="sm" onClick={() => setCalsOpen(true)} />
          <Button variant="primary" size="sm" icon={Plus} label="Nuevo" onClick={() => setDialog({ event: null, day: selectedDay })} testId="cal-new" />
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
        <input type="text" className="fs-cal__quick-input" placeholder="Añade un evento en tus palabras…" value={quick} onChange={(e) => setQuick(e.target.value)} data-testid="cal-quick" />
        <Button type="submit" variant="secondary" size="sm" label="Añadir" disabled={!quick.trim()} loading={parsing} />
      </form>

      <div className="fs-cal__bar">
        <div className="fs-cal__nav">
          <IconButton icon={ChevronLeft} label="Anterior" size="sm" onClick={() => setCursor((d) => step(view, d, -1))} />
          <button type="button" className="fs-chip" onClick={goToday}>
            Hoy
          </button>
          <IconButton icon={ChevronRight} label="Siguiente" size="sm" onClick={() => setCursor((d) => step(view, d, 1))} />
          <h2 className="fs-cal__title">{title}</h2>
        </div>
        <div className="fs-cal__views" role="group" aria-label="Vista">
          {(['month', 'week', 'agenda', 'year'] as View[]).map((v) => (
            <button key={v} type="button" className="fs-chip" data-on={view === v || undefined} onClick={() => setView(v)}>
              {v === 'month' ? 'Mes' : v === 'week' ? 'Semana' : v === 'agenda' ? 'Agenda' : 'Año'}
            </button>
          ))}
        </div>
      </div>

      {calendars && calendars.length > 1 && (
        <div className="fs-cal__filters" role="group" aria-label="Calendarios visibles">
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

      {truncated && <p className="fs-cal__warn">Hay más repeticiones de las que el servidor expande; acota el rango.</p>}

      {(!events || !calendars) && <Skeleton label="Cargando el calendario" count={5} height="64px" />}

      {events && calendars && view === 'month' && (
        <div className="fs-cal__month">
          <div className="fs-cal__weekdays">
            {WEEKDAYS.map((w) => (
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
          <aside className="fs-cal__day" aria-label="Día seleccionado">
            <header className="fs-cal__day-head">
              <span>{fmtDay(selectedDay)}</span>
              <Button variant="ghost" size="sm" icon={Plus} label="Evento" onClick={() => setDialog({ event: null, day: selectedDay })} />
            </header>
            {dayEvents.length === 0 && <p className="fs-cal__empty">Nada ese día.</p>}
            {dayEvents.map((ev) => (
              <button key={ev.uid} type="button" className="fs-cal__day-ev" onClick={() => setDialog({ event: ev, day: null })}>
                <span className="fs-cal__dot" style={{ background: ev.color || '#5b8abf' }} aria-hidden="true" />
                <span className="fs-cal__day-when">{ev.allDay ? 'todo el día' : `${fmtTime(ev.dtstart)}–${fmtTime(ev.dtend)}`}</span>
                <span className="fs-cal__day-title">{ev.summary || '(sin título)'}</span>
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
                  <span className="fs-cal__wday-name">{WEEKDAYS[i]}</span>
                  <span className="fs-cal__wday-num">{d.getDate()}</span>
                </header>
                <div className="fs-cal__wday-list">
                  {list.map((ev) => (
                    <EventChip key={ev.uid} ev={ev} onOpen={() => setDialog({ event: ev, day: null })} />
                  ))}
                  <button type="button" className="fs-cal__wday-add" onClick={() => setDialog({ event: null, day: key })} aria-label={`Nuevo evento el ${key}`}>
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
                    <span className="fs-cal__dot" style={{ background: ev.color || '#5b8abf' }} aria-hidden="true" />
                    <span className="fs-cal__day-when">{ev.allDay ? 'todo el día' : `${fmtTime(ev.dtstart)}–${fmtTime(ev.dtend)}`}</span>
                    <span className="fs-cal__day-title">{ev.summary || '(sin título)'}</span>
                    {ev.location && (
                      <span className="fs-cal__day-loc">
                        <MapPin size={10} aria-hidden="true" /> {ev.location}
                      </span>
                    )}
                  </button>
                ))}
              </section>
            ))}
          {byDay.size === 0 && <EmptyState icon={CalendarDays} title="Nada en los próximos 60 días" body="Añade algo arriba en tus palabras o con N." />}
        </div>
      )}

      {events && calendars && view === 'year' && (
        <div className="fs-cal__year">
          {MONTHS.map((name, m) => {
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
            say('Guardado.');
            refresh();
          }}
          onDeleted={() => {
            setDialog(null);
            say('Borrado.');
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
