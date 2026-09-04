import { getJson } from './api';

/**
 * The app's settings (`/api/auth/settings`) as Studio needs them: the
 * keybinds the person configured in the previous interface apply here too,
 * merged over the same defaults (static/js/keyboard-shortcuts.js).
 */

export const DEFAULT_KEYBINDS: Record<string, string> = {
  search: 'ctrl+k',
  toggle_sidebar: 'ctrl+b',
  new_session: 'ctrl+alt+n',
  fav_session: 'ctrl+alt+f',
  delete_session: 'ctrl+alt+d',
  cancel: 'escape',
  tts: 'alt+shift+t',
  incognito: 'ctrl+alt+i',
  settings: 'ctrl+,',
  focus_input: 'ctrl+/',
  open_calendar: 'ctrl+alt+c',
  open_compare: '',
  open_cookbook: '',
  open_research: '',
  open_gallery: '',
  open_library: '',
  open_memory: '',
  open_notes: '',
  open_tasks: '',
  open_theme: '',
};

let cached: Promise<Record<string, unknown>> | null = null;

export function getSettings(signal?: AbortSignal): Promise<Record<string, unknown>> {
  if (!cached) {
    cached = getJson<Record<string, unknown>>('/api/auth/settings', signal).catch((e) => {
      cached = null;
      throw e;
    });
  }
  return cached;
}

export function invalidateSettings(): void {
  cached = null;
}

export async function getKeybinds(): Promise<Record<string, string>> {
  try {
    const s = await getSettings();
    const raw = s.keybinds && typeof s.keybinds === 'object' ? (s.keybinds as Record<string, unknown>) : {};
    const out = { ...DEFAULT_KEYBINDS };
    for (const [k, v] of Object.entries(raw)) if (typeof v === 'string') out[k] = v;
    return out;
  } catch {
    return { ...DEFAULT_KEYBINDS };
  }
}

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

/** AltGr on Windows arrives as Ctrl+Alt: never let it fire a shortcut while
 *  someone types a character on a non-US layout. */
function isAltGr(e: KeyboardEvent): boolean {
  if (IS_MAC) return false;
  return e.ctrlKey && e.altKey && !e.metaKey && e.key.length === 1 && !/^[a-z0-9]$/i.test(e.key);
}

export function matchesCombo(e: KeyboardEvent, combo: string): boolean {
  if (!combo) return false;
  if (isAltGr(e)) return false;
  const parts = combo.toLowerCase().split('+');
  const needCtrl = parts.includes('ctrl');
  const needAlt = parts.includes('alt');
  const needShift = parts.includes('shift');
  const key = parts.filter((p) => p !== 'ctrl' && p !== 'alt' && p !== 'shift')[0] ?? '';
  if (needCtrl !== (e.ctrlKey || e.metaKey)) return false;
  if (needAlt !== e.altKey) return false;
  if (needShift !== e.shiftKey) return false;
  return e.key.toLowerCase() === key;
}
