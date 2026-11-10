/**
 * The prompts this browser sent, newest last, for ↑/↓ in an empty composer
 * (or one still showing a recalled prompt) — like a shell's history. Kept in
 * localStorage (a per-device convenience: lost in a private window, never
 * required), at most `MAX` entries, consecutive repeats folded, slash
 * commands and steering kept like any other line.
 */
export const HISTORY_KEY = 'faustus.prompt-history';
export const MAX = 200;

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadHistory(): string[] {
  try {
    const raw = storage()?.getItem(HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

/** The history with `text` appended (trimmed, no consecutive repeat, capped). */
export function pushed(history: string[], text: string): string[] {
  const line = text.trim();
  if (!line) return history;
  const next = history[history.length - 1] === line ? history.slice() : [...history, line];
  return next.slice(-MAX);
}

export function remember(text: string): void {
  try {
    storage()?.setItem(HISTORY_KEY, JSON.stringify(pushed(loadHistory(), text)));
  } catch {
    /* full or blocked storage: history is a convenience */
  }
}

/** Where ↑ (`-1`) or ↓ (`+1`) leads from `index` (`history.length` = the
 *  fresh, empty line). Returns the new index and the text to show. */
export function step(history: string[], index: number, dir: -1 | 1): { index: number; text: string } {
  const at = Math.max(0, Math.min(history.length, index + dir));
  return { index: at, text: at === history.length ? '' : history[at] };
}

/** ↑/↓ browse the history only when the composer is empty or still shows
 *  the entry it recalled (so arrows keep moving the caret in a real draft). */
export function browsing(draft: string, history: string[], index: number): boolean {
  return !draft.trim() || (index < history.length && draft === history[index]);
}
