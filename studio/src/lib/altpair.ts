/**
 * OBJ-47 — pure logic behind "compare two alternatives": reading the unified
 * diff the server sends (`src/alternatives.py::compare_pair`), laying it out
 * side by side, filtering/ordering the file list, moving through it with the
 * keyboard, and the small formatters the screen shares. No DOM, no fetch, so
 * `studio/checks/alternatives_pair.check.mjs` can drive all of it.
 */
import type { PairFile, PairOverlap, PairStatus } from '../adapters/alternatives';

export type DiffRowKind = 'hunk' | 'ctx' | 'add' | 'del' | 'note';

export interface DiffRow {
  kind: DiffRowKind;
  text: string;
  oldNo: number | null;
  newNo: number | null;
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** The unified diff as rows with line numbers. The two header lines
 *  (`--- a/x`, `+++ b/x`) are dropped: the file name is shown by the screen.
 *  A line that merely *looks* like a header (`--- foo` removed from the
 *  file) is still a row, because only the first two lines are headers. */
export function parseUnifiedDiff(diff: string): DiffRow[] {
  if (!diff) return [];
  const lines = diff.split('\n');
  let start = 0;
  if (lines.length >= 2 && lines[0].startsWith('--- ') && lines[1].startsWith('+++ ')) start = 2;
  const rows: DiffRow[] = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;
  for (let i = start; i < lines.length; i += 1) {
    const line = lines[i];
    const hunk = HUNK.exec(line);
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[2]);
      inHunk = true;
      rows.push({ kind: 'hunk', text: line, oldNo: null, newNo: null });
      continue;
    }
    if (!inHunk) continue;
    if (line.startsWith('\\')) {
      rows.push({ kind: 'note', text: line.replace(/^\\\s?/, ''), oldNo: null, newNo: null });
    } else if (line.startsWith('+')) {
      rows.push({ kind: 'add', text: line.slice(1), oldNo: null, newNo });
      newNo += 1;
    } else if (line.startsWith('-')) {
      rows.push({ kind: 'del', text: line.slice(1), oldNo, newNo: null });
      oldNo += 1;
    } else if (line === '' && i === lines.length - 1) {
      continue; // the trailing split artefact, not a blank context line
    } else {
      rows.push({ kind: 'ctx', text: line.slice(1), oldNo, newNo });
      oldNo += 1;
      newNo += 1;
    }
  }
  return rows;
}

export interface SideCell {
  no: number | null;
  text: string;
  kind: 'ctx' | 'del' | 'add' | 'empty';
}

export interface SideRow {
  hunk: string | null;
  note: string | null;
  left: SideCell;
  right: SideCell;
}

const EMPTY: SideCell = { no: null, text: '', kind: 'empty' };

/** Side by side: a run of removed lines is laid next to the run of added
 *  lines that follows it, one pair per row, the shorter side padded. */
export function toSideBySide(rows: DiffRow[]): SideRow[] {
  const out: SideRow[] = [];
  let i = 0;
  while (i < rows.length) {
    const row = rows[i];
    if (row.kind === 'hunk') {
      out.push({ hunk: row.text, note: null, left: EMPTY, right: EMPTY });
      i += 1;
    } else if (row.kind === 'note') {
      out.push({ hunk: null, note: row.text, left: EMPTY, right: EMPTY });
      i += 1;
    } else if (row.kind === 'ctx') {
      out.push({
        hunk: null, note: null,
        left: { no: row.oldNo, text: row.text, kind: 'ctx' },
        right: { no: row.newNo, text: row.text, kind: 'ctx' },
      });
      i += 1;
    } else {
      const dels: DiffRow[] = [];
      const adds: DiffRow[] = [];
      while (i < rows.length && rows[i].kind === 'del') { dels.push(rows[i]); i += 1; }
      while (i < rows.length && rows[i].kind === 'add') { adds.push(rows[i]); i += 1; }
      const n = Math.max(dels.length, adds.length);
      for (let k = 0; k < n; k += 1) {
        const d = dels[k];
        const a = adds[k];
        out.push({
          hunk: null, note: null,
          left: d ? { no: d.oldNo, text: d.text, kind: 'del' } : EMPTY,
          right: a ? { no: a.newNo, text: a.text, kind: 'add' } : EMPTY,
        });
      }
    }
  }
  return out;
}

export type StatusFilter = 'all' | PairStatus | 'conflict';

export interface FileFilter {
  query: string;
  status: StatusFilter;
}

/** Files matching the search box and the status chip. `conflict` is the
 *  overlap verdict, not a file status; it is a filter because "which of
 *  these will collide" is the first thing a person wants to see. */
export function filterFiles(files: PairFile[], filter: FileFilter): PairFile[] {
  const q = filter.query.trim().toLowerCase();
  return files.filter((f) => {
    if (q && !f.path.toLowerCase().includes(q) && !(f.old_path ?? '').toLowerCase().includes(q)) return false;
    if (filter.status === 'all') return true;
    if (filter.status === 'conflict') return f.overlap === 'conflict';
    return f.status === filter.status;
  });
}

export interface StatusCount {
  all: number;
  added: number;
  removed: number;
  changed: number;
  renamed: number;
  conflict: number;
}

export function statusCounts(files: PairFile[]): StatusCount {
  const out: StatusCount = { all: files.length, added: 0, removed: 0, changed: 0, renamed: 0, conflict: 0 };
  for (const f of files) {
    out[f.status] += 1;
    if (f.overlap === 'conflict') out.conflict += 1;
  }
  return out;
}

/** Arrow/Home/End movement through the file list. -1 when the list is empty;
 *  any other key leaves the index alone. The index never leaves the list. */
export function moveIndex(index: number, key: string, length: number): number {
  if (length <= 0) return -1;
  const at = Math.min(Math.max(index, 0), length - 1);
  switch (key) {
    case 'ArrowDown': return Math.min(length - 1, at + 1);
    case 'ArrowUp': return Math.max(0, at - 1);
    case 'Home': return 0;
    case 'End': return length - 1;
    default: return at;
  }
}

/** The path that stays selected after the list changes (filter typed,
 *  another pair loaded): the same file when it is still there, else the
 *  first one, else none. */
export function keepSelection(files: PairFile[], selected: string | null): string | null {
  if (files.length === 0) return null;
  if (selected && files.some((f) => f.path === selected)) return selected;
  return files[0].path;
}

/** The pair to show first: whatever was chosen if both still exist and are
 *  different, else the first two alternatives. `null` below two. */
export function defaultPair(ids: string[], current: [string, string] | null): [string, string] | null {
  if (ids.length < 2) return null;
  if (current && current[0] !== current[1] && ids.includes(current[0]) && ids.includes(current[1])) return current;
  return [ids[0], ids[1]];
}

/** Picking the other side's alternative on one side swaps them instead of
 *  producing the "same alternative twice" pair the server refuses. */
export function pickSide(pair: [string, string], side: 0 | 1, id: string, ids: string[]): [string, string] {
  const next: [string, string] = [pair[0], pair[1]];
  const other = side === 0 ? 1 : 0;
  if (next[other] === id) next[other] = pair[side];
  next[side] = id;
  if (next[0] === next[1]) {
    const alt = ids.find((x) => x !== next[side]);
    if (alt) next[other] = alt;
  }
  return next;
}

export function swapPair(pair: [string, string]): [string, string] {
  return [pair[1], pair[0]];
}

export function formatBytes(bytes: number | null): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** What to say instead of a diff when there is none to show, or null when
 *  the file has a diff (possibly cut short — that is `diff_truncated`). */
export type NoDiffReason = 'binary' | 'too_large' | 'same_content' | 'empty' | null;

export function noDiffReason(file: PairFile): NoDiffReason {
  if (file.binary) return 'binary';
  if (file.too_large) return 'too_large';
  if (file.status === 'renamed' && !file.diff) return 'same_content';
  if (!file.diff) return 'empty';
  return null;
}

export function overlapTone(overlap: PairOverlap | null): 'bad' | 'warn' | 'good' | 'quiet' {
  switch (overlap) {
    case 'conflict': return 'bad';
    case 'unknown': return 'warn';
    case 'mergeable': return 'warn';
    case 'identical': return 'good';
    default: return 'quiet';
  }
}

/** The single character shown beside a file: a glyph is not enough on its
 *  own (colour must never carry the meaning), so the screen always pairs it
 *  with the status word. */
export function statusGlyph(status: PairStatus): string {
  switch (status) {
    case 'added': return '+';
    case 'removed': return '−';
    case 'renamed': return '→';
    default: return '~';
  }
}
