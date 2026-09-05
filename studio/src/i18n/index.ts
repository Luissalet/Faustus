import { useSyncExternalStore } from 'react';
import { en } from './en';
import { es } from './es';

/**
 * The interface language.
 *
 * English is the default and the source: every string in Studio is written
 * in English at the call site (`t('Save')`) and the Spanish dictionary maps
 * it (`es.ts`). A key with no entry falls back to itself, so a missing
 * translation is an English string, never a blank or a code. Interpolation is
 * `{name}`; plurals go through `tn(n, one, other)`.
 *
 * The choice lives in localStorage (instant on the next load) and in the
 * user's prefs on the server (`ui_language`, so it follows them to another
 * browser); the shell reconciles the two on mount. Changing it re-keys the
 * whole tree from AppShell, which is the honest way to re-render static
 * labels without threading a context through every component.
 */

export type Lang = 'en' | 'es';

export const LANGS: { value: Lang; label: string }[] = [
  { value: 'en', label: 'English' },
  { value: 'es', label: 'Español' },
];

const KEY = 'faustus_studio_lang';
const DICTS: Record<Lang, Record<string, string>> = { en, es };

function isLang(v: unknown): v is Lang {
  return v === 'en' || v === 'es';
}

function read(): Lang {
  try {
    const stored = window.localStorage.getItem(KEY);
    if (isLang(stored)) return stored;
  } catch {
    /* private mode */
  }
  return 'en';
}

let current: Lang = read();
const listeners = new Set<() => void>();

function apply(lang: Lang): void {
  current = lang;
  try {
    document.documentElement.lang = lang;
  } catch {
    /* no document */
  }
  for (const fn of listeners) fn();
}

apply(current);

export function getLang(): Lang {
  return current;
}

/** Change the language: local first (instant), then the server pref. */
export function setLang(lang: Lang, { persist = true }: { persist?: boolean } = {}): void {
  if (!isLang(lang) || lang === current) return;
  try {
    window.localStorage.setItem(KEY, lang);
  } catch {
    /* private mode */
  }
  apply(lang);
  if (persist) {
    void fetch('/api/prefs/ui_language', {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: lang }),
    }).catch(() => {});
  }
}

/** The server's copy, applied if the user picked one there (another browser). */
export async function syncLangFromServer(): Promise<void> {
  try {
    const response = await fetch('/api/prefs/ui_language', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (!response.ok) return;
    const data = (await response.json()) as { value?: unknown };
    if (isLang(data.value) && data.value !== current) setLang(data.value, { persist: false });
  } catch {
    /* offline: the local choice stands */
  }
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useLang(): Lang {
  return useSyncExternalStore(subscribe, getLang, getLang);
}

type Vars = Record<string, string | number>;

function fill(text: string, vars?: Vars): string {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m));
}

/** Translate one string. The key is the English text. */
export function t(key: string, vars?: Vars): string {
  const dict = DICTS[current];
  return fill(dict[key] ?? key, vars);
}

/** A count with its noun: `tn(n, '{n} note', '{n} notes')`. */
export function tn(n: number, one: string, other: string, vars?: Vars): string {
  return t(n === 1 ? one : other, { n, ...(vars ?? {}) });
}

/** The BCP-47 tag for dates and numbers. */
export function locale(): string {
  return current === 'es' ? 'es-ES' : 'en-GB';
}
