/**
 * The pilot flag (UI-021).
 *
 * `localStorage` plus a query parameter, and nothing on the server. Faustus
 * has no feature-flag mechanism at all, and building an administrable one is
 * its own project; for a single-user pilot this is enough and the rollback is
 * a reload.
 *
 * It is a flag with an expiry, not a second interface. Each migrated screen
 * retires its legacy counterpart, and when the last one goes, this file goes
 * with it (DECISIONES_UI.md §4).
 */

const KEY = 'faustus_studio_shell';

export function readFlagFromUrl(): boolean | null {
  const value = new URLSearchParams(window.location.search).get('shell');
  if (value === 'studio') return true;
  if (value === 'legacy') return false;
  return null;
}

export function isStudioEnabled(): boolean {
  const fromUrl = readFlagFromUrl();
  if (fromUrl !== null) {
    setStudioEnabled(fromUrl);
    return fromUrl;
  }
  try {
    return window.localStorage.getItem(KEY) === '1';
  } catch {
    // Private windows and blocked site data throw on access rather than
    // returning null. Off is the safe answer: the legacy UI still works.
    return false;
  }
}

export function setStudioEnabled(enabled: boolean): void {
  try {
    if (enabled) window.localStorage.setItem(KEY, '1');
    else window.localStorage.removeItem(KEY);
  } catch {
    /* nothing to do: the URL parameter still works for this page load */
  }
}
