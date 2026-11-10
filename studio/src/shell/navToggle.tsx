import { PanelLeft } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { DEFAULT_KEYBINDS, getKeybinds, subscribeSettings } from '../adapters/settings';
import { t } from '../i18n';
import { setNavPeek, toggleNavHidden, useNavHidden, useNavPeek } from './navSize';

/** The sidebar's shortcut as the person has it in Settings, kept current when they change it. */
export function useNavCombo(): string {
  const [combo, setCombo] = useState(DEFAULT_KEYBINDS.toggle_nav ?? '');
  useEffect(() => {
    let alive = true;
    const load = () =>
      void getKeybinds().then((binds) => {
        if (alive) setCombo(binds.toggle_nav ?? '');
      });
    load();
    const off = subscribeSettings(load);
    return () => {
      alive = false;
      off();
    };
  }, []);
  return combo;
}

/** `ctrl+.` as it is read: Ctrl+. */
export function comboLabel(combo: string): string {
  return combo
    .split('+')
    .map((part) => (part === 'ctrl' ? 'Ctrl' : part === 'alt' ? 'Alt' : part === 'shift' ? t('Shift') : part.length === 1 ? part.toUpperCase() : part))
    .join('+');
}

/**
 * Hide the sidebar or bring it back. A button that was pressed leaves with
 * its bar, so the keyboard moves to the one that took its place; focus that
 * was anywhere else (the composer when the shortcut is used, the palette)
 * is left alone.
 */
export function flipNav(): void {
  const from = document.activeElement;
  const fromTheBar = from instanceof HTMLElement && (from.classList.contains('fs-nav__toggle') || from.closest('.fs-nav') !== null);
  toggleNavHidden();
  if (!fromTheBar) return;
  requestAnimationFrame(() => {
    const active = document.activeElement;
    if (active && active !== document.body && active.getClientRects().length > 0) return;
    const next = [...document.querySelectorAll<HTMLElement>('.fs-nav__toggle')].find((button) => button.getClientRects().length > 0);
    next?.focus({ preventScroll: true });
  });
}

/** The one control for it, wherever it is drawn: on the sidebar, where the sidebar was, or on the window's own bar. */
export function NavToggle({ className }: { className?: string }) {
  const hidden = useNavHidden();
  const peeking = useNavPeek();
  const combo = useNavCombo();
  const label = peeking ? t('Keep the sidebar open') : hidden ? t('Show the sidebar') : t('Hide the sidebar');
  return (
    <button
      type="button"
      className={className ? `fs-nav__toggle ${className}` : 'fs-nav__toggle'}
      onClick={flipNav}
      aria-label={label}
      aria-expanded={!hidden}
      title={combo ? `${label} (${comboLabel(combo)})` : label}
      data-testid="nav-toggle"
    >
      <PanelLeft size={15} aria-hidden="true" />
    </button>
  );
}

/** How long the mouse rests on the edge before the sidebar looks out, and how long after leaving it goes. */
const PEEK_IN_MS = 140;
const PEEK_OUT_MS = 260;
/** Where the mouse last was over the edge or the sidebar, and where it was when Escape sent the sidebar away:
 *  a mouse still resting there has not reached for it again. */
let mouseAt: { x: number; y: number } | null = null;
let dismissedAt: { x: number; y: number } | null = null;

/**
 * The left edge of the page while the sidebar is put away: the mouse on it
 * shows the sidebar over the page, without bringing it back for good, and
 * the sidebar goes when the mouse leaves it. A menu opened from the sidebar
 * keeps it there until the menu closes; Escape lets it go; its own button
 * keeps it. Touch and the keyboard have the button and the shortcut.
 */
export function NavPeek() {
  const peeking = useNavPeek();
  const wait = useRef(0);
  useEffect(() => () => { window.clearTimeout(wait.current); setNavPeek(false); }, []);
  useEffect(() => {
    if (!peeking) return;
    const nav = document.querySelector<HTMLElement>('.fs-shell > .fs-nav');
    if (!nav) return;
    let timer = 0;
    // A menu or a panel opened from the sidebar is drawn outside it; while one is open the sidebar stays.
    const menuOpen = () => document.querySelector('[role="menu"], [role="dialog"]') !== null;
    const settle = () => {
      if (nav.matches(':hover') || document.querySelector('.fs-nav-edge:hover')) return;
      if (menuOpen()) { timer = window.setTimeout(settle, 400); return; }
      setNavPeek(false);
    };
    const leave = () => { window.clearTimeout(timer); timer = window.setTimeout(settle, PEEK_OUT_MS); };
    const enter = () => window.clearTimeout(timer);
    const move = (event: PointerEvent) => { mouseAt = { x: event.clientX, y: event.clientY }; };
    const key = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || menuOpen()) return;
      dismissedAt = mouseAt;
      setNavPeek(false);
    };
    nav.addEventListener('pointerleave', leave);
    nav.addEventListener('pointerenter', enter);
    nav.addEventListener('pointermove', move);
    window.addEventListener('keydown', key);
    // The mouse may have gone before the sidebar drew under it.
    timer = window.setTimeout(settle, 900);
    return () => {
      window.clearTimeout(timer);
      nav.removeEventListener('pointerleave', leave);
      nav.removeEventListener('pointerenter', enter);
      nav.removeEventListener('pointermove', move);
      window.removeEventListener('keydown', key);
    };
  }, [peeking]);
  return (
    <div
      className="fs-nav-edge"
      aria-hidden="true"
      data-testid="nav-edge"
      onPointerEnter={(event) => {
        if (event.pointerType !== 'mouse') return;
        // With the sidebar gone the edge is under a mouse that has not moved: that is not reaching for it.
        if (dismissedAt && dismissedAt.x === event.clientX && dismissedAt.y === event.clientY) return;
        dismissedAt = null;
        mouseAt = { x: event.clientX, y: event.clientY };
        window.clearTimeout(wait.current);
        wait.current = window.setTimeout(() => setNavPeek(true), PEEK_IN_MS);
      }}
      onPointerLeave={() => window.clearTimeout(wait.current)}
    />
  );
}
