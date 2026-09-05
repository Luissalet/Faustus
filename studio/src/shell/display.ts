import { useSyncExternalStore } from 'react';

/**
 * What the transcript shows — the previous interface's Appearance toggles
 * that were about the chat rather than the layout: the thinking blocks,
 * emojis in replies, blurred secrets, the welcome screen, full width.
 * Per browser (`faustus_studio_display`); the layout toggles of the old
 * sidebar have no counterpart because Studio has no such sidebar.
 */
export interface Display {
  thinking: boolean;
  emojis: boolean;
  blur: boolean;
  welcome: boolean;
  fullWidth: boolean;
}
const DEFAULTS: Display = { thinking: true, emojis: true, blur: false, welcome: true, fullWidth: false };
const KEY = 'faustus_studio_display';

function read(): Display {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Display>) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}
let current = read();
const listeners = new Set<() => void>();

export function getDisplay(): Display {
  return current;
}
export function setDisplay(patch: Partial<Display>): void {
  current = { ...current, ...patch };
  try {
    window.localStorage.setItem(KEY, JSON.stringify(current));
  } catch {
    /* private mode */
  }
  document.documentElement.toggleAttribute('data-fullwidth', current.fullWidth);
  for (const fn of listeners) fn();
}
export function useDisplay(): Display {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    getDisplay,
    getDisplay,
  );
}
document.documentElement.toggleAttribute('data-fullwidth', current.fullWidth);

/* ── text treatments ── */

const EMOJI = /[\u{1F300}-\u{1FAFF}\u{1F1E6}-\u{1F1FF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}\u{1F900}-\u{1F9FF}]/gu;
/** Strip emojis from a reply (the "text-only emojis" toggle). */
export function stripEmojis(text: string): string {
  return text.replace(EMOJI, '').replace(/ {2,}/g, ' ');
}

/** The same patterns static/js/censor.js blurred: mails, keys, tokens, credentials, private keys, hashes, JWTs, LAN addresses. */
export const SENSITIVE: RegExp[] = [
  /\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b/g,
  /\b(sk-[a-zA-Z0-9]{20,}|pk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36,}|gho_[a-zA-Z0-9]{36,}|glpat-[a-zA-Z0-9_-]{20,}|xox[bpras]-[a-zA-Z0-9-]{10,}|npm_[a-zA-Z0-9]{36,}|AKIA[A-Z0-9]{12,})\b/g,
  /Bearer\s+[A-Za-z0-9._-]{20,}/g,
  /(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key|client[_-]?secret)\s*[:=]\s*["']?[^\s"'<]{4,}["']?/gi,
  /-----BEGIN\s[\w\s]*PRIVATE KEY-----[\s\S]*?-----END\s[\w\s]*PRIVATE KEY-----/g,
  /\b[0-9a-f]{32,}\b/gi,
  /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g,
  /\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?::\d+)?\b/g,
];
/** Split `text` into plain and sensitive runs. */
export function findSensitive(text: string): { text: string; sensitive: boolean }[] {
  const hits: [number, number][] = [];
  for (const re of SENSITIVE) {
    re.lastIndex = 0;
    for (const m of text.matchAll(re)) {
      if (m[0]) hits.push([m.index ?? 0, (m.index ?? 0) + m[0].length]);
    }
  }
  if (!hits.length) return [{ text, sensitive: false }];
  hits.sort((a, b) => a[0] - b[0]);
  const out: { text: string; sensitive: boolean }[] = [];
  let pos = 0;
  for (const [s, e] of hits) {
    if (e <= pos) continue;
    const start = Math.max(s, pos);
    if (start > pos) out.push({ text: text.slice(pos, start), sensitive: false });
    out.push({ text: text.slice(start, e), sensitive: true });
    pos = e;
  }
  if (pos < text.length) out.push({ text: text.slice(pos), sensitive: false });
  return out;
}
