import { useSyncExternalStore } from 'react';
import { setEffect, type EffectName } from './effects';
import { reapplyThemeChoice } from './theme';

/**
 * Appearance — the previous interface's theme editor, kept compatible.
 *
 * A theme is `{ name, colors, font, density, textSize, frosted, bgPattern,
 * bgEffectColor, bgEffectIntensity, bgEffectSize }` stored under the same
 * localStorage key (`odysseus-theme`) and server pref (`theme`) the old
 * editor used, so a theme picked there is the theme here and vice versa.
 * Colours land on <html> as `--bg --fg --panel --border --red`, exactly as
 * before, and Studio reads them through `user-theme.css` when the root
 * carries `data-theme-source="faustus"`. `name: 'studio'` (the default)
 * means Studio's own palette: no variables, no attribute.
 *
 * Custom themes: `odysseus-custom-themes` + `/api/prefs/custom-themes`, up
 * to eight, as before.
 */

export interface Colors {
  bg: string;
  fg: string;
  panel: string;
  border: string;
  red: string;
}
/** A built-in choice, or the family name of a file dropped in static/fonts/custom/. */
export type FontChoice = 'studio' | 'mono' | 'sans' | 'serif' | 'opendyslexic' | (string & {});
export type Density = 'compact' | 'comfortable' | 'spacious';
export interface Theme {
  name: string;
  colors?: Colors;
  font?: FontChoice;
  density?: Density;
  textSize?: '100' | '125';
  frosted?: boolean;
  bgPattern?: EffectName;
  bgEffectColor?: string;
  bgEffectIntensity?: number;
  bgEffectSize?: number;
}

export const PRESETS: Record<string, Colors> = {
  dark: { bg: '#282c34', fg: '#9cdef2', panel: '#111111', border: '#355a66', red: '#e06c75' },
  light: { bg: '#f0ebe3', fg: '#5a5248', panel: '#faf6f0', border: '#d4cdc2', red: '#c47d5a' },
  midnight: { bg: '#0d1117', fg: '#c9d1d9', panel: '#161b22', border: '#30363d', red: '#f85149' },
  paper: { bg: '#faf8f5', fg: '#3b3836', panel: '#ffffff', border: '#d5d0c8', red: '#c5ac4a' },
  cyberpunk: { bg: '#0a0a0f', fg: '#0ff0fc', panel: '#12101a', border: '#9b30ff', red: '#e040fb' },
  retrowave: { bg: '#1a1a2e', fg: '#e94560', panel: '#16213e', border: '#533483', red: '#e94560' },
  forest: { bg: '#1b2a1b', fg: '#a8d5a2', panel: '#142414', border: '#3d6b3d', red: '#7cb871' },
  ocean: { bg: '#0b1a2c', fg: '#64d2ff', panel: '#091422', border: '#1e5074', red: '#4facfe' },
  ume: { bg: '#2b1b2e', fg: '#f5c2e7', panel: '#1e1420', border: '#6c4675', red: '#f5a0c0' },
  copper: { bg: '#1c1410', fg: '#e8c39e', panel: '#140f0a', border: '#7a5533', red: '#d4764e' },
  terminal: { bg: '#000000', fg: '#00ff41', panel: '#0a0a0a', border: '#003b00', red: '#00ff41' },
  organs: { bg: '#0a0406', fg: '#efe1c8', panel: '#15080a', border: '#3a1519', red: '#c83240' },
  lavender: { bg: '#f3eef8', fg: '#3d3551', panel: '#faf7ff', border: '#cec3de', red: '#9b6dcc' },
  gpt: { bg: '#212121', fg: '#ececec', panel: '#171717', border: '#424242', red: '#949494' },
  claude: { bg: '#262624', fg: '#f5f4f0', panel: '#30302e', border: '#4a4a47', red: '#c6613f' },
  cute: { bg: '#fff0f5', fg: '#d4608a', panel: '#fff8fa', border: '#f0c0d0', red: '#ff6b9d' },
};
/** The effect each built-in theme came with, as before. */
export const PRESET_PATTERN: Record<string, EffectName> = {
  dark: 'none',
  light: 'dots',
  midnight: 'rain',
  paper: 'dots',
  cyberpunk: 'synapse',
  retrowave: 'embers',
  forest: 'petals',
  ocean: 'constellations',
  terminal: 'perlin-flow',
  organs: 'rain',
  ume: 'petals',
  cute: 'sparkles',
};
export const PRESET_EFFECT_COLOR: Record<string, string> = { midnight: '#ffffff', organs: '#451616', cute: '#ff8cb8', ume: '#f5a0c0' };

export const FONT_MAP: Record<string, string> = {
  studio: '',
  mono: "'Fira Code', ui-monospace, monospace",
  sans: "system-ui, -apple-system, 'Segoe UI', sans-serif",
  serif: "Georgia, 'Times New Roman', serif",
  opendyslexic: "'OpenDyslexic', sans-serif",
};

/* Custom fonts: files in static/fonts/custom/, listed by /api/fonts/custom. */
export interface FontVariant {
  url: string;
  format: string;
}
let customFonts: Record<string, FontVariant[]> = {};
const injected = new Set<string>();
function injectFont(family: string, variants: FontVariant[]): void {
  if (injected.has(family)) return;
  const fmt: Record<string, string> = { woff2: 'woff2', woff: 'woff', ttf: 'truetype', otf: 'opentype' };
  const style = document.createElement('style');
  style.dataset.customFont = family;
  style.textContent = variants.map((v) => `@font-face { font-family: '${family.replace(/'/g, '')}'; src: url('${v.url}') format('${fmt[v.format] ?? v.format}'); font-display: swap; }`).join('\n');
  document.head.appendChild(style);
  injected.add(family);
}
export async function loadCustomFonts(): Promise<Record<string, FontVariant[]>> {
  try {
    const r = await fetch('/api/fonts/custom', { credentials: 'same-origin' });
    if (r.ok) {
      const d = (await r.json()) as { fonts?: Record<string, FontVariant[]> };
      customFonts = d.fonts ?? {};
      applyTheme(current);
    }
  } catch {
    /* none */
  }
  return customFonts;
}

const KEY = 'odysseus-theme';
const CUSTOM_KEY = 'odysseus-custom-themes';
export const MAX_CUSTOM = 8;

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}
function writeJson(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode */
  }
}

function normalize(raw: unknown): Theme {
  const o = (raw && typeof raw === 'object' ? raw : {}) as Partial<Theme> & { name?: string };
  let name = o.name || 'studio';
  if (name === 'chatgpt') name = 'gpt';
  if (name === 'sakura') name = 'ume';
  const t: Theme = { name };
  if (o.colors && typeof o.colors === 'object') t.colors = { ...(PRESETS[name] ?? PRESETS.dark), ...o.colors };
  else if (PRESETS[name]) t.colors = PRESETS[name];
  if (o.font && typeof o.font === 'string') t.font = o.font;
  if (o.density === 'compact' || o.density === 'spacious') t.density = o.density;
  if (o.textSize === '125') t.textSize = '125';
  if (o.frosted) t.frosted = true;
  if (o.bgPattern && o.bgPattern !== 'none') t.bgPattern = o.bgPattern;
  if (o.bgEffectColor) t.bgEffectColor = o.bgEffectColor;
  if (typeof o.bgEffectIntensity === 'number') t.bgEffectIntensity = o.bgEffectIntensity;
  if (typeof o.bgEffectSize === 'number') t.bgEffectSize = o.bgEffectSize;
  return t;
}

let current: Theme = normalize(readJson(KEY, null));
let customThemes: Record<string, Theme> = readJson(CUSTOM_KEY, {});
const listeners = new Set<() => void>();
function emit() {
  for (const fn of listeners) fn();
}

/** Put the theme on the document. Idempotent; safe before the shell mounts. */
export function applyTheme(t: Theme): void {
  const html = document.documentElement;
  const roots = document.querySelectorAll<HTMLElement>('.fs-app');
  const own = t.name === 'studio' || !t.colors;
  for (const k of ['--bg', '--fg', '--panel', '--border', '--red']) html.style.removeProperty(k);
  if (!own && t.colors) {
    html.style.setProperty('--bg', t.colors.bg);
    html.style.setProperty('--fg', t.colors.fg);
    html.style.setProperty('--panel', t.colors.panel);
    html.style.setProperty('--border', t.colors.border);
    html.style.setProperty('--red', t.colors.red);
  }
  roots.forEach((r) => {
    if (own) r.removeAttribute('data-theme-source');
    else r.setAttribute('data-theme-source', 'faustus');
  });
  html.setAttribute('data-theme-source', own ? 'studio' : 'faustus');
  // A palette decides light or dark by its own background; Studio's palette
  // goes back to the user's choice (system / light / dark).
  if (own) reapplyThemeChoice();
  else if (t.colors) html.setAttribute('data-theme', isLight(t.colors.bg) ? 'light' : 'dark');
  const font = t.font ?? 'studio';
  let family = FONT_MAP[font];
  if (family === undefined && customFonts[font]) {
    injectFont(font, customFonts[font]);
    family = `'${font}', sans-serif`;
  }
  if (family) html.style.setProperty('--fs-font-ui', family);
  else html.style.removeProperty('--fs-font-ui');
  if (t.density && t.density !== 'comfortable') html.setAttribute('data-density', t.density);
  else html.removeAttribute('data-density');
  if (t.textSize === '125') html.setAttribute('data-text-size', '125');
  else html.removeAttribute('data-text-size');
  if (t.frosted) html.setAttribute('data-frosted', '');
  else html.removeAttribute('data-frosted');
  if (t.bgEffectColor) html.style.setProperty('--bg-effect-color', t.bgEffectColor);
  else html.style.removeProperty('--bg-effect-color');
  html.style.setProperty('--bg-effect-intensity', String(t.bgEffectIntensity ?? 1));
  html.style.setProperty('--bg-effect-size', String(t.bgEffectSize ?? 1));
  setEffect(t.bgPattern ?? 'none');
}

export function getTheme(): Theme {
  return current;
}

export function setTheme(next: Theme, { persist = true }: { persist?: boolean } = {}): void {
  current = normalize(next);
  applyTheme(current);
  writeJson(KEY, current);
  emit();
  if (persist) {
    void fetch('/api/prefs/theme', { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: current }) }).catch(() => {});
  }
}

/** Pick a built-in preset: its colours, its effect and its effect colour, as the old grid did. */
export function themeFromPreset(name: string): Theme {
  const colors = PRESETS[name];
  if (!colors) return { name: 'studio' };
  const t: Theme = { name, colors, font: current.font, density: current.density, textSize: current.textSize, frosted: current.frosted };
  const pattern = PRESET_PATTERN[name] ?? 'none';
  if (pattern !== 'none') t.bgPattern = pattern;
  if (PRESET_EFFECT_COLOR[name]) t.bgEffectColor = PRESET_EFFECT_COLOR[name];
  return t;
}

export async function syncAppearanceFromServer(): Promise<void> {
  try {
    const r = await fetch('/api/prefs/theme', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const d = (await r.json()) as { value?: unknown };
    if (d.value && typeof d.value === 'object' && JSON.stringify(normalize(d.value)) !== JSON.stringify(current)) setTheme(normalize(d.value), { persist: false });
  } catch {
    /* offline */
  }
  try {
    const r = await fetch('/api/prefs/custom-themes', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const d = (await r.json()) as { value?: unknown };
    if (d.value && typeof d.value === 'object' && Object.keys(d.value as object).length) {
      customThemes = d.value as Record<string, Theme>;
      writeJson(CUSTOM_KEY, customThemes);
      emit();
    }
  } catch {
    /* offline */
  }
}

export function getCustomThemes(): Record<string, Theme> {
  return customThemes;
}
function syncCustom(): void {
  writeJson(CUSTOM_KEY, customThemes);
  emit();
  void fetch('/api/prefs/custom-themes', { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: customThemes }) }).catch(() => {});
}
/** Save the current look under a name. Returns false when the shelf (8) is full. */
export function saveCustomTheme(name: string): boolean {
  const clean = name.trim().slice(0, 32);
  if (!clean) return false;
  if (!customThemes[clean] && Object.keys(customThemes).length >= MAX_CUSTOM) return false;
  customThemes = { ...customThemes, [clean]: { ...current, name: clean } };
  syncCustom();
  return true;
}
export function deleteCustomTheme(name: string): void {
  const next = { ...customThemes };
  delete next[name];
  customThemes = next;
  syncCustom();
}

export function exportThemeJson(): string {
  return JSON.stringify(current, null, 2);
}
export function importThemeJson(text: string): Theme {
  const parsed = JSON.parse(text) as unknown;
  const t = normalize(parsed);
  if (!t.colors) throw new Error('no colours');
  if (!PRESETS[t.name] && !customThemes[t.name]) t.name = 'imported';
  return t;
}

/* ── colour helpers (the same maths as the old editor) ── */

export function isLight(hex: string): boolean {
  return hexToHSL(hex)[2] > 55;
}

export function hexToHSL(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  const n = m ? parseInt(m[1], 16) : 0;
  const r = ((n >> 16) & 255) / 255;
  const g = ((n >> 8) & 255) / 255;
  const b = (n & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  let h = 0;
  let s = 0;
  const l = (max + min) / 2;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else h = ((r - g) / d + 4) / 6;
  }
  return [h * 360, s * 100, l * 100];
}
export function hslToHex(h: number, s: number, l: number): string {
  h = ((h % 360) + 360) % 360;
  s = Math.max(0, Math.min(100, s)) / 100;
  l = Math.max(0, Math.min(100, l)) / 100;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  const toHex = (v: number) => Math.round(v * 255).toString(16).padStart(2, '0');
  return `#${toHex(f(0))}${toHex(f(8))}${toHex(f(4))}`;
}
export type Harmony = 'complementary' | 'analogous' | 'triadic' | 'monochromatic';
export function harmonyColors(accent: string, harmony: Harmony, mode: 'dark' | 'light'): Colors {
  const [h, s] = hexToHSL(accent);
  const dark = mode === 'dark';
  let bgH: number, bgS: number, bgL: number, fgS: number, fgL: number, panelL: number, borderH: number, borderS: number, borderL: number;
  if (harmony === 'complementary') {
    bgH = h; bgS = Math.max(s * 0.15, 3); bgL = dark ? 13 : 95; fgL = dark ? 85 : 15; fgS = Math.max(s * 0.2, 5); panelL = dark ? 8 : 98; borderH = h; borderS = Math.max(s * 0.25, 8); borderL = dark ? 28 : 75;
  } else if (harmony === 'analogous') {
    bgH = (h - 30 + 360) % 360; bgS = Math.max(s * 0.12, 3); bgL = dark ? 14 : 95; fgL = dark ? 84 : 18; fgS = Math.max(s * 0.15, 5); panelL = dark ? 9 : 97; borderH = (h + 30) % 360; borderS = Math.max(s * 0.3, 10); borderL = dark ? 30 : 72;
  } else if (harmony === 'triadic') {
    bgH = (h + 240) % 360; bgS = Math.max(s * 0.1, 2); bgL = dark ? 13 : 96; fgL = dark ? 86 : 14; fgS = Math.max(s * 0.18, 5); panelL = dark ? 8 : 99; borderH = (h + 120) % 360; borderS = Math.max(s * 0.2, 8); borderL = dark ? 28 : 74;
  } else {
    bgH = h; bgS = Math.max(s * 0.08, 2); bgL = dark ? 12 : 96; fgL = dark ? 87 : 13; fgS = Math.max(s * 0.15, 5); panelL = dark ? 7 : 99; borderH = h; borderS = Math.max(s * 0.2, 6); borderL = dark ? 26 : 76;
  }
  return { bg: hslToHex(bgH, bgS, bgL), fg: hslToHex(h, fgS, fgL), panel: hslToHex(bgH, bgS * 0.6, panelL), border: hslToHex(borderH, borderS, borderL), red: accent };
}

export function useAppearance(): { theme: Theme; custom: Record<string, Theme> } {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    getSnapshot,
    getSnapshot,
  );
}
let snap = { theme: current, custom: customThemes };
function getSnapshot() {
  if (snap.theme !== current || snap.custom !== customThemes) snap = { theme: current, custom: customThemes };
  return snap;
}

/** Applied once at module load so the first paint already wears the theme. */
applyTheme(current);
