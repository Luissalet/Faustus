import { useEffect, useRef, useState } from 'react';
import { Repeat } from 'lucide-react';
import { t } from '../../i18n';
import { addDays, ds, fgFor, fmtTime, parseStamp, type CalEvent } from '../../adapters/calendar';
import { backgroundOf } from '../../lib/paint';
import {
  DAY_MINUTES,
  HOUR_HEIGHTS,
  clockOf,
  layout,
  minutesFromY,
  minutesOf,
  moveTo,
  resizeTo,
  spanFromDrag,
  spanOn,
  yFromMinutes,
  type Span,
  type Zoom,
} from '../../lib/agenda';

/**
 * The week, with hours.
 *
 * Seven columns of chips said what was on Wednesday; this says when. Drag
 * an event to move it, drag its bottom edge to make it longer, drag empty
 * space to make a new one — all snapped to the quarter hour, all in local
 * time, and the arithmetic lives in `lib/agenda.ts` where it can be tested.
 */

type Drag =
  | { kind: 'create'; day: string; from: number; at: number }
  | { kind: 'move'; ev: CalEvent; day: string; span: Span; grab: number }
  | { kind: 'resize'; ev: CalEvent; day: string; span: Span };

export interface WeekProps {
  start: Date;
  byDay: Map<string, CalEvent[]>;
  today: string;
  zoom: Zoom;
  weekdays: string[];
  defaultColor: string;
  onOpen: (ev: CalEvent) => void;
  onCreate: (day: string, span: Span) => void;
  onMove: (ev: CalEvent, day: string, span: Span) => void;
}

const HOURS = Array.from({ length: 24 }, (_, h) => h);

export function Week({ start, byDay, today, zoom, weekdays, defaultColor, onOpen, onCreate, onMove }: WeekProps) {
  const hourHeight = HOUR_HEIGHTS[zoom];
  const gridRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [now, setNow] = useState(() => minutesOf(new Date()));
  const days = Array.from({ length: 7 }, (_, i) => addDays(start, i));

  useEffect(() => {
    const timer = window.setInterval(() => setNow(minutesOf(new Date())), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  /** Where a pointer is, as a day column and minutes from that midnight. */
  const where = (clientX: number, clientY: number): { day: string; minutes: number } | null => {
    const grid = gridRef.current;
    if (!grid) return null;
    const rect = grid.getBoundingClientRect();
    const column = Math.min(6, Math.max(0, Math.floor(((clientX - rect.left) / rect.width) * 7)));
    const minutes = minutesFromY(clientY - rect.top + grid.scrollTop, hourHeight);
    return { day: ds(days[column]), minutes: Math.min(Math.max(minutes, 0), DAY_MINUTES) };
  };

  useEffect(() => {
    if (!drag) return;
    const move = (event: PointerEvent) => {
      const at = where(event.clientX, event.clientY);
      if (!at) return;
      setDrag((cur) => {
        if (!cur) return cur;
        if (cur.kind === 'create') return { ...cur, at: at.minutes, day: cur.day };
        if (cur.kind === 'move') return { ...cur, day: at.day, span: moveTo(cur.span, at.minutes - cur.grab) };
        return { ...cur, span: resizeTo(cur.span, at.minutes) };
      });
    };
    const up = () => {
      setDrag((cur) => {
        if (!cur) return null;
        if (cur.kind === 'create') {
          const span = spanFromDrag(cur.from, cur.at);
          onCreate(cur.day, span);
        } else {
          onMove(cur.ev, cur.day, cur.span);
        }
        return null;
      });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
    };
  });

  const ghost = drag?.kind === 'create' ? spanFromDrag(drag.from, drag.at) : null;
  const held = drag && drag.kind !== 'create' ? drag : null;

  return (
    <div className="fs-wk" style={{ ['--fs-hour' as string]: `${hourHeight}px` }} data-dragging={drag ? '' : undefined} data-testid="cal-week">
      <div className="fs-wk__head">
        <span className="fs-wk__gutter" aria-hidden="true" />
        {days.map((d, i) => {
          const key = ds(d);
          return (
            <div key={key} className="fs-wk__dayhead" data-today={key === today || undefined}>
              <span className="fs-wk__dayname">{weekdays[i]}</span>
              <span className="fs-wk__daynum">{d.getDate()}</span>
            </div>
          );
        })}
      </div>

      <div className="fs-wk__allday">
        <span className="fs-wk__gutter">{t('all day')}</span>
        {days.map((d) => {
          const key = ds(d);
          const all = (byDay.get(key) ?? []).filter((ev) => ev.allDay);
          return (
            <div key={key} className="fs-wk__alldaycell">
              {all.map((ev) => (
                <button key={ev.uid} type="button" className="fs-cal-ev fs-wk__allev" style={tile(ev.color || defaultColor)} onClick={() => onOpen(ev)} title={ev.summary}>
                  <span className="fs-cal-ev__title">{ev.summary || t('(untitled)')}</span>
                </button>
              ))}
            </div>
          );
        })}
      </div>

      <div className="fs-wk__body">
        <div className="fs-wk__hours" aria-hidden="true">
          {HOURS.map((h) => (
            <span key={h} className="fs-wk__hour">
              {clockOf(h * 60)}
            </span>
          ))}
        </div>
        <div className="fs-wk__grid" ref={gridRef}>
          {days.map((d) => {
            const key = ds(d);
            const dayStart = new Date(d);
            dayStart.setHours(0, 0, 0, 0);
            const timed = (byDay.get(key) ?? []).filter((ev) => !ev.allDay);
            const placed = layout(timed, (ev) => spanOn(parseStamp(ev.dtstart), parseStamp(ev.dtend), dayStart));
            return (
              <div
                key={key}
                className="fs-wk__col"
                data-today={key === today || undefined}
                onPointerDown={(event) => {
                  if (event.button !== 0 || (event.target as HTMLElement).closest('.fs-wk__ev')) return;
                  const at = where(event.clientX, event.clientY);
                  if (at) setDrag({ kind: 'create', day: at.day, from: at.minutes, at: at.minutes });
                }}
              >
                {key === today && <span className="fs-wk__now" style={{ insetBlockStart: `${yFromMinutes(now, hourHeight)}px` }} aria-label={t('Now')} />}
                {placed
                  // The one being dragged is drawn by the column it is over,
                  // which may not be the one it belongs to yet.
                  .filter(({ item }) => !(held && held.ev.uid === item.uid))
                  .map(({ item: ev, span, column, columns }) => (
                    <Tile
                      key={ev.uid}
                      ev={ev}
                      span={span}
                      column={column}
                      columns={columns}
                      hourHeight={hourHeight}
                      defaultColor={defaultColor}
                      onOpen={() => !drag && onOpen(ev)}
                      onGrab={(minutes) => setDrag({ kind: 'move', ev, day: key, span, grab: minutes - span.from })}
                      onResize={() => setDrag({ kind: 'resize', ev, day: key, span })}
                      where={where}
                    />
                  ))}
                {held && held.day === key && (
                  <Tile
                    live
                    ev={held.ev}
                    span={held.span}
                    column={0}
                    columns={1}
                    hourHeight={hourHeight}
                    defaultColor={defaultColor}
                    onOpen={() => undefined}
                    onGrab={() => undefined}
                    onResize={() => undefined}
                    where={where}
                  />
                )}
                {ghost && drag?.kind === 'create' && drag.day === key && (
                  <span
                    className="fs-wk__ghost"
                    style={{ insetBlockStart: `${yFromMinutes(ghost.from, hourHeight)}px`, blockSize: `${yFromMinutes(ghost.to - ghost.from, hourHeight)}px` }}
                  >
                    {clockOf(ghost.from)}–{clockOf(ghost.to)}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Tile({
  ev,
  span,
  column,
  columns,
  hourHeight,
  defaultColor,
  live,
  onOpen,
  onGrab,
  onResize,
  where,
}: {
  ev: CalEvent;
  span: Span;
  column: number;
  columns: number;
  hourHeight: number;
  defaultColor: string;
  live?: boolean;
  onOpen: () => void;
  onGrab: (minutes: number) => void;
  onResize: () => void;
  where: (x: number, y: number) => { day: string; minutes: number } | null;
}) {
  return (
    <button
      type="button"
      className="fs-wk__ev fs-cal-ev"
      data-live={live || undefined}
      style={{
        ...tile(ev.color || defaultColor),
        insetBlockStart: `${yFromMinutes(span.from, hourHeight)}px`,
        blockSize: `${Math.max(yFromMinutes(span.to - span.from, hourHeight), 14)}px`,
        insetInlineStart: `${(column / columns) * 100}%`,
        inlineSize: `calc(${(1 / columns) * 100}% - 3px)`,
      }}
      title={`${ev.summary} · ${fmtTime(ev.dtstart)}`}
      onClick={onOpen}
      onPointerDown={(event) => {
        if (event.button !== 0 || live) return;
        const at = where(event.clientX, event.clientY);
        if (!at) return;
        event.stopPropagation();
        onGrab(at.minutes);
      }}
    >
      <span className="fs-cal-ev__time">{clockOf(span.from)}</span>
      <span className="fs-cal-ev__title">{ev.summary || t('(untitled)')}</span>
      {ev.rrule && <Repeat size={10} aria-label={t('Repeats')} />}
      <span
        className="fs-wk__handle"
        aria-hidden="true"
        onPointerDown={(event) => {
          if (event.button !== 0 || live) return;
          event.stopPropagation();
          onResize();
        }}
      />
    </button>
  );
}

/**
 * An event's colour — or the picture it carries in place of one. The
 * previous interface stored it in the same field with the same `bg:`
 * sentinel a note uses, so an event given a picture there looks the same
 * here.
 */
export function tile(color: string): Record<string, string> {
  const image = backgroundOf(color);
  if (!image) return { background: color, color: fgFor(color) };
  return {
    backgroundImage: `linear-gradient(color-mix(in srgb, var(--fs-canvas) 45%, transparent), color-mix(in srgb, var(--fs-canvas) 45%, transparent)), url("${image}")`,
    backgroundSize: 'cover',
    backgroundPosition: 'center',
    color: 'var(--fs-text-1)',
  };
}
