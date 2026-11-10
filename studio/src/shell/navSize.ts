import { useCallback, useEffect, useState, useSyncExternalStore, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react';

/**
 * The sidebar's width, in the person's hands.
 *
 * Drag its edge to resize; squeeze it under the collapse point and it folds
 * to the icon rail; drag the rail back out and it opens again. Double-click
 * (or Enter on the handle) puts it back to the default. Below 1280px the rail
 * is the default anyway, as before — but a width the person chose wins over
 * the breakpoint, in both directions.
 *
 * It can also be put away altogether: a button on the sidebar hides it, and
 * the same button, where the sidebar was, brings it back as it stood.
 *
 * Stored in localStorage as `{ mode, width, back }`: `auto` follows the
 * breakpoint; `wide`, `rail` and `hidden` are choices, and `back` is what
 * `hidden` returns to. The state lives in this module, not in a component,
 * so the button, the shortcut, the palette and the agent all move the same
 * sidebar.
 */

export type NavMode = 'wide' | 'rail' | 'hidden';
type Shown = 'auto' | 'wide' | 'rail';
export type NavStored = { mode: Shown | 'hidden'; width: number; back: Shown };

const KEY = 'faustus_studio_nav';
export const DEFAULT_WIDTH = 236;
const MIN_WIDE = 168;
const MAX_WIDE = 400;
const COLLAPSE_AT = 120;
const RAIL_WIDTH = 72;
/** What is left where the sidebar was: room for the button that brings it back. */
const HIDDEN_WIDTH = 44;
const NARROW = '(max-width: 1279px)';

const shown = (v: unknown): Shown | null => (v === 'auto' || v === 'wide' || v === 'rail' ? v : null);

/** What a stored value means; anything unreadable is the default sidebar. */
export function parseNav(raw: string | null): NavStored {
  try {
    if (raw) {
      const v = JSON.parse(raw) as Partial<NavStored>;
      const width = typeof v.width === 'number' && v.width >= MIN_WIDE && v.width <= MAX_WIDE ? v.width : DEFAULT_WIDTH;
      const back = shown(v.back) ?? 'auto';
      if (v.mode === 'hidden') return { mode: 'hidden', width, back };
      const mode = shown(v.mode) ?? 'auto';
      return { mode, width, back: mode };
    }
  } catch {
    /* junk */
  }
  return { mode: 'auto', width: DEFAULT_WIDTH, back: 'auto' };
}

/** Putting it away remembers how it stood… */
export function hideNav(s: NavStored): NavStored {
  return s.mode === 'hidden' ? s : { mode: 'hidden', width: s.width, back: s.mode };
}

/** …and bringing it back puts it as it stood. */
export function showNav(s: NavStored): NavStored {
  return s.mode === 'hidden' ? { mode: s.back, width: s.width, back: s.back } : s;
}

function read(): NavStored {
  try {
    return parseNav(window.localStorage.getItem(KEY));
  } catch {
    /* private mode */
    return parseNav(null);
  }
}

function write(v: NavStored): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(v));
  } catch {
    /* private mode */
  }
}

let state: NavStored | null = null;
const listeners = new Set<() => void>();
const current = (): NavStored => (state ??= read());
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};

function commit(next: NavStored): void {
  if (next === current()) return;
  state = next;
  write(next);
  listeners.forEach((listener) => listener());
}

export function navHidden(): boolean {
  return current().mode === 'hidden';
}

export function setNavHidden(hidden: boolean): void {
  commit(hidden ? hideNav(current()) : showNav(current()));
}

export function toggleNavHidden(): void {
  setNavHidden(!navHidden());
}

/** Whether the sidebar is put away, for whatever draws the button. */
export function useNavHidden(): boolean {
  return useSyncExternalStore(subscribe, navHidden, () => false);
}

export function useNavSize() {
  const stored = useSyncExternalStore(subscribe, current, current);
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

  const mode: NavMode =
    stored.mode === 'hidden' ? 'hidden' : live !== null ? (live < COLLAPSE_AT ? 'rail' : 'wide') : stored.mode === 'auto' ? (narrow ? 'rail' : 'wide') : stored.mode;
  const width = live !== null && mode === 'wide' ? Math.max(MIN_WIDE, Math.min(MAX_WIDE, live)) : stored.width;

  const choose = useCallback((next: Shown, nextWidth: number) => commit({ mode: next, width: nextWidth, back: next }), []);

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
        if (last < COLLAPSE_AT) choose('rail', width);
        else choose('wide', Math.max(MIN_WIDE, Math.min(MAX_WIDE, Math.round(last))));
      };
      target.addEventListener('pointermove', move);
      target.addEventListener('pointerup', up);
      target.addEventListener('pointercancel', up);
    },
    [choose, mode, width],
  );

  const reset = useCallback(() => choose('auto', DEFAULT_WIDTH), [choose]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      const step = event.shiftKey ? 40 : 16;
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        if (mode === 'rail') return;
        const next = width - step;
        if (next < COLLAPSE_AT + 40) choose('rail', width);
        else choose('wide', Math.max(MIN_WIDE, next));
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        if (mode === 'rail') choose('wide', width);
        else choose('wide', Math.min(MAX_WIDE, width + step));
      } else if (event.key === 'Enter' || event.key === 'Home') {
        event.preventDefault();
        reset();
      }
    },
    [choose, mode, reset, width],
  );

  const style = { '--fs-nav-w': mode === 'hidden' ? `${HIDDEN_WIDTH}px` : mode === 'rail' ? `${RAIL_WIDTH}px` : `${width}px` } as CSSProperties;
  return { mode, width, resizing, style, handle: { onPointerDown, onDoubleClick: reset, onKeyDown } };
}
