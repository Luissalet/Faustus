/**
 * Hoja's pure logic (no DOM, no React): cell addresses, the row/column geometry the virtual grid scrolls over,
 * the selection, tab-separated clipboard text, fill steps and the scales the charts are drawn with.
 * The spreadsheet itself (formulas, formats, files) is the server's (src/hoja.py).
 */

export const DEFAULT_COL_W = 96;
export const DEFAULT_ROW_H = 24;
export const MAX_ROWS = 1_048_576;
export const MAX_COLS = 16_384;

export interface Sel { r0: number; c0: number; r1: number; c1: number }
export interface Cell { r: number; c: number; v: unknown; shown: string; formula: string | null; sid: number; ov: Record<string, unknown> | null }
export type Style = { b?: number; i?: number; c?: string; bg?: string; al?: 'l' | 'c' | 'r'; wrap?: number; nf?: string; bd?: string };

export function colLetters(n: number): string {
  let s = '';
  for (let x = n; x > 0; x = Math.floor((x - 1) / 26)) s = String.fromCharCode(65 + ((x - 1) % 26)) + s;
  return s;
}
export function colNumber(letters: string): number {
  let n = 0;
  for (const ch of letters.toUpperCase()) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n;
}
export const addr = (r: number, c: number): string => colLetters(c) + r;
export function parseAddr(text: string): { r: number; c: number } | null {
  const m = /^\$?([A-Za-z]{1,3})\$?(\d{1,7})$/.exec(text.trim());
  return m ? { r: Number(m[2]), c: colNumber(m[1]) } : null;
}
export const normSel = (s: Sel): Sel => ({ r0: Math.min(s.r0, s.r1), c0: Math.min(s.c0, s.c1), r1: Math.max(s.r0, s.r1), c1: Math.max(s.c0, s.c1) });
export const rangeText = (s: Sel): string => {
  const n = normSel(s);
  return n.r0 === n.r1 && n.c0 === n.c1 ? addr(n.r0, n.c0) : `${addr(n.r0, n.c0)}:${addr(n.r1, n.c1)}`;
};
export const inSel = (s: Sel, r: number, c: number): boolean => { const n = normSel(s); return r >= n.r0 && r <= n.r1 && c >= n.c0 && c <= n.c1; };
export const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v));

/* ── geometry: cumulative offsets with sparse overrides, so 100 000 rows cost nothing ── */
export interface Axis {
  /** Start offset (px) of 1-based index i. */
  start(i: number): number;
  size(i: number): number;
  /** The index whose span holds the offset (px), clamped to [1, count]. */
  at(px: number): number;
  total: number;
  count: number;
}
export function makeAxis(count: number, base: number, overrides: Record<string, number>, hidden: Iterable<number> = []): Axis {
  const sizes = new Map<number, number>();
  for (const [k, v] of Object.entries(overrides)) { const i = Number(k); if (i >= 1 && i <= count && Number.isFinite(v)) sizes.set(i, v); }
  for (const h of hidden) if (h >= 1 && h <= count) sizes.set(h, 0);
  const idx = [...sizes.keys()].sort((a, b) => a - b);
  const cum: number[] = [];                 // cum[k] = total extra px of the overrides idx[0..k]
  let extra = 0;
  for (const i of idx) { extra += (sizes.get(i) as number) - base; cum.push(extra); }
  const before = (i: number): number => {   // extra px of the overrides strictly before index i
    let lo = 0, hi = idx.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (idx[mid] < i) lo = mid + 1; else hi = mid; }
    return lo === 0 ? 0 : cum[lo - 1];
  };
  const size = (i: number): number => sizes.get(i) ?? base;
  const start = (i: number): number => (i - 1) * base + before(i);
  const total = count * base + extra;
  const at = (px: number): number => {
    if (px <= 0) return 1;
    let lo = 1, hi = count;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (start(mid) <= px) lo = mid; else hi = mid - 1; }
    return lo;
  };
  return { start, size, at, total, count };
}

/* ── clipboard ── */
export function toTsv(rows: string[][]): string {
  return rows.map((row) => row.map((cell) => (/[\t\n"]/.test(cell) ? '"' + cell.replace(/"/g, '""') + '"' : cell)).join('\t')).join('\n');
}
export function parseTsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = '', q = false;
  const src = text.replace(/\r\n?/g, '\n');
  for (let i = 0; i < src.length; i += 1) {
    const ch = src[i];
    if (q) {
      if (ch === '"') { if (src[i + 1] === '"') { cell += '"'; i += 1; } else q = false; } else cell += ch;
    } else if (ch === '"' && cell === '') q = true;
    else if (ch === '\t') { row.push(cell); cell = ''; }
    else if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += ch;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
export function toHtmlTable(rows: string[][], styles: (Style | undefined)[][]): string {
  const body = rows.map((row, r) => '<tr>' + row.map((cell, c) => {
    const st = styles[r]?.[c];
    const css = [st?.b ? 'font-weight:bold' : '', st?.i ? 'font-style:italic' : '', st?.c ? `color:${st.c}` : '', st?.bg ? `background:${st.bg}` : '',
      st?.al ? `text-align:${st.al === 'l' ? 'left' : st.al === 'c' ? 'center' : 'right'}` : ''].filter(Boolean).join(';');
    return `<td${css ? ` style="${css}"` : ''}>${escapeHtml(cell)}</td>`;
  }).join('') + '</tr>').join('');
  return `<table><tbody>${body}</tbody></table>`;
}
/** Cells of the first <table> in pasted HTML, as text (used when the clipboard has HTML but no usable plain text). */
export function tableFromHtml(html: string): string[][] | null {
  if (typeof DOMParser === 'undefined') return null;
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const table = doc.querySelector('table');
  if (!table) return null;
  return [...table.querySelectorAll('tr')].map((tr) => [...tr.querySelectorAll('th,td')].map((td) => (td.textContent ?? '').trim()));
}

/* ── fill handle ── */
export type FillPlan = { range: string; from: string; series: boolean; step: number }[];
/**
 * What dragging the fill handle sends: one `fill` op per column (dragging down) or row (dragging right) of the
 * source, each continuing a numeric pattern when the source has two or more numbers in that line.
 */
export function planFill(src: Sel, to: Sel, value: (r: number, c: number) => unknown): FillPlan {
  const s = normSel(src), t = normSel(to);
  const plan: FillPlan = [];
  const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
  if (t.r1 > s.r1 && t.c0 === s.c0 && t.c1 === s.c1) {
    for (let c = s.c0; c <= s.c1; c += 1) {
      const a = value(s.r1 - 1, c), b = value(s.r1, c);
      const series = s.r1 > s.r0 && num(a) && num(b);
      plan.push({ range: `${addr(s.r1, c)}:${addr(t.r1, c)}`, from: addr(s.r1, c), series, step: series ? (b as number) - (a as number) : 1 });
    }
  } else if (t.c1 > s.c1 && t.r0 === s.r0 && t.r1 === s.r1) {
    for (let r = s.r0; r <= s.r1; r += 1) {
      const a = value(r, s.c1 - 1), b = value(r, s.c1);
      const series = s.c1 > s.c0 && num(a) && num(b);
      plan.push({ range: `${addr(r, s.c1)}:${addr(r, t.c1)}`, from: addr(r, s.c1), series, step: series ? (b as number) - (a as number) : 1 });
    }
  }
  return plan;
}

/* ── status-bar numbers ── */
export function summarise(values: unknown[]): { count: number; sum: number; avg: number } {
  let count = 0, sum = 0;
  for (const v of values) if (typeof v === 'number' && Number.isFinite(v)) { count += 1; sum += v; }
  return { count, sum, avg: count ? sum / count : 0 };
}

/* ── charts ── */
export function niceTicks(min: number, max: number, target = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) { min = min === 0 ? 0 : Math.min(0, min); max = max === 0 ? 1 : max; }
  const span = max - min;
  const raw = span / Math.max(1, target);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const first = Math.floor(min / step) * step;
  const out: number[] = [];
  for (let k = 0; out.length < 12; k += 1) {
    const v = Math.round((first + k * step) / step * 1e6) / 1e6 * step;
    out.push(v);
    if (v >= max) break;
  }
  return out;
}
export function compactNumber(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e9) return +(n / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return +(n / 1e6).toFixed(2) + 'M';
  if (a >= 1e4) return +(n / 1e3).toFixed(1) + 'k';
  return String(+n.toFixed(2));
}
