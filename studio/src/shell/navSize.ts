import { useCallback, useEffect, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react';

/**
 * The sidebar's width, in the person's hands.
 *
 * Drag its edge to resize; squeeze it under the collapse point and it folds
 * to the icon rail; drag the rail back out and it opens again. Double-click
 * (or Enter on the handle) puts it back to the default. Below 1280px the rail
 * is the default anyway, as before — but a width the person chose wins over
 * the breakpoint, in both directions.
 *
 * Stored in localStorage as `{ mode, width }`: `auto` follows the
 * breakpoint; `wide` and `rail` are choices.
 */

export type NavMode = 'wide' | 'rail';
type Stored = { mode: 'auto' | NavMode; width: number };

const KEY = 'faustus_studio_nav';
export const DEFAULT_WIDTH = 236;
const MIN_WIDE = 168;
const MAX_WIDE = 400;
const COLLAPSE_AT = 120;
const RAIL_WIDTH = 72;
const NARROW = '(max-width: 1279px)';

function read(): Stored {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) {
      const v = JSON.parse(raw) as Partial<Stored>;
      const width = typeof v.width === 'number' && v.width >= MIN_WIDE && v.width <= MAX_WIDE ? v.width : DEFAULT_WIDTH;
      const mode = v.mode === 'wide' || v.mode === 'rail' ? v.mode : 'auto';
      return { mode, width };
    }
  } catch {
    /* private mode or junk */
  }
  return { mode: 'auto', width: DEFAULT_WIDTH };
}

function write(v: Stored): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(v));
  } catch {
    /* private mode */
  }
}

export function useNavSize() {
  const [stored, setStored] = useState<Stored>(read);
  const [narrow, setNarrow] = useState(() => (typeof window !== 'undefined' ? window.matchMedia(NARROW).matches : false));
  const [resizing, setResizing] = useState(false);
  // While dragging, the live width (not persisted until the pointer lifts).
  const [live, setLive] = useState<number | null>(null);

  useEffect(() => {
    const mq = window.matchMedia(NARROW);
    const on = () => setNarrow(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);

  const mode: NavMode = live !== null ? (live < COLLAPSE_AT ? 'rail' : 'wide') : stored.mode === 'auto' ? (narrow ? 'rail' : 'wide') : stored.mode;
  const width = live !== null && mode === 'wide' ? Math.max(MIN_WIDE, Math.min(MAX_WIDE, live)) : stored.width;

  const commit = useCallback((next: Stored) => {
    setStored(next);
    write(next);
  }, []);

  const onPointerDown = useCallback(
    (event: PointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const target = event.currentTarget;
      target.setPointerCapture(event.pointerId);
      setResizing(true);
      const startX = event.clientX;
      const startWidth = mode === 'rail' ? RAIL_WIDTH : width;
      let last = startWidth;
      const move = (e: globalThis.PointerEvent) => {
        last = startWidth + (e.clientX - startX);
        setLive(last);
      };
      const up = () => {
        target.removeEventListener('pointermove', move);
        target.removeEventListener('pointerup', up);
        target.removeEventListener('pointercancel', up);
        setResizing(false);
        setLive(null);
        if (last < COLLAPSE_AT) commit({ mode: 'rail', width });
        else commit({ mode: 'wide', width: Math.max(MIN_WIDE, Math.min(MAX_WIDE, Math.round(last))) });
      };
      target.addEventListener('pointermove', move);
      target.addEventListener('pointerup', up);
      target.addEventListener('pointercancel', up);
    },
    [commit, mode, width],
  );

  const reset = useCallback(() => commit({ mode: 'auto', width: DEFAULT_WIDTH }), [commit]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      const step = event.shiftKey ? 40 : 16;
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        if (mode === 'rail') return;
        const next = width - step;
        if (next < COLLAPSE_AT + 40) commit({ mode: 'rail', width });
        else commit({ mode: 'wide', width: Math.max(MIN_WIDE, next) });
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        if (mode === 'rail') commit({ mode: 'wide', width });
        else commit({ mode: 'wide', width: Math.min(MAX_WIDE, width + step) });
      } else if (event.key === 'Enter' || event.key === 'Home') {
        event.preventDefault();
        reset();
      }
    },
    [commit, mode, reset, width],
  );

  const style = { '--fs-nav-w': mode === 'rail' ? `${RAIL_WIDTH}px` : `${width}px` } as CSSProperties;
  return { mode, width, resizing, style, handle: { onPointerDown, onDoubleClick: reset, onKeyDown } };
}
