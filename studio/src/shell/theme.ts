import { useSyncExternalStore } from 'react';

/**
 * Light, dark, or whatever the system says.
 *
 * tokens.css already resolves in that order — `:root[data-theme]` first,
 * then `prefers-color-scheme`, then the dark base — so the whole feature is
 * one attribute on <html>. The choice lives in localStorage (applied before
 * the first paint of the next load) and in the user's prefs (`ui_theme`), the
 * same pair the language uses. "System" removes the attribute rather than
 * writing a third value: the media query is the system preference.
 */

export type ThemeChoice = 'system' | 'light' | 'dark';

const KEY = 'faustus_studio_theme';

function isChoice(v: unknown): v is ThemeChoice {
  return v === 'system' || v === 'light' || v === 'dark';
}

function read(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(KEY);
    if (isChoice(stored)) return stored;
  } catch {
    /* private mode */
  }
  return 'system';
}

let current: ThemeChoice = read();
const listeners = new Set<() => void>();

function apply(choice: ThemeChoice): void {
  current = choice;
  try {
    if (choice === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', choice);
  } catch {
    /* no document */
  }
  for (const fn of listeners) fn();
}

apply(current);

export function getTheme(): ThemeChoice {
  return current;
}

export function setTheme(choice: ThemeChoice, { persist = true }: { persist?: boolean } = {}): void {
  if (!isChoice(choice) || choice === current) return;
  try {
    window.localStorage.setItem(KEY, choice);
  } catch {
    /* private mode */
  }
  apply(choice);
  if (persist) {
    void fetch('/api/prefs/ui_theme', {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: choice }),
    }).catch(() => {});
  }
}

export async function syncThemeFromServer(): Promise<void> {
  try {
    const response = await fetch('/api/prefs/ui_theme', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (!response.ok) return;
    const data = (await response.json()) as { value?: unknown };
    if (isChoice(data.value) && data.value !== current) setTheme(data.value, { persist: false });
  } catch {
    /* offline: the local choice stands */
  }
}

export function useTheme(): ThemeChoice {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    getTheme,
    getTheme,
  );
}

/** Put the saved choice back on <html> (after a palette forced its own light or dark). */
export function reapplyThemeChoice(): void {
  apply(current);
}

/** Clean up when the shell unmounts, so the previous interface is not left with our attribute. */
export function clearThemeAttribute(): void {
  try {
    document.documentElement.removeAttribute('data-theme');
  } catch {
    /* no document */
  }
}
