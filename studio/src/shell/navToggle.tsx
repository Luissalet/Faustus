import { PanelLeft } from 'lucide-react';
import { useEffect, useState } from 'react';
import { DEFAULT_KEYBINDS, getKeybinds, subscribeSettings } from '../adapters/settings';
import { t } from '../i18n';
import { toggleNavHidden, useNavHidden } from './navSize';

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
  const combo = useNavCombo();
  const label = hidden ? t('Show the sidebar') : t('Hide the sidebar');
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
