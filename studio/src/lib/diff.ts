/**
 * Line and word diffs for the document editor's review mode. Ported from the
 * legacy `document.js` (`_computeLineDiff`, `_buildDiffChunks`, `simpleDiff`).
 */

export type DiffEntry = { type: 'equal' | 'insert' | 'delete'; line: string };

/** LCS line diff, old → new. Returns null when the texts are too large to diff comfortably. */
export function lineDiff(oldText: string, newText: string): DiffEntry[] | null {
  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');
  const m = oldLines.length, n = newLines.length;
  if (m * n > 4_000_000) return null;
  const dp: Uint32Array[] = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = oldLines[i - 1] === newLines[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  const entries: DiffEntry[] = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      entries.push({ type: 'equal', line: oldLines[i - 1] });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      entries.push({ type: 'insert', line: newLines[j - 1] });
      j--;
    } else {
      entries.push({ type: 'delete', line: oldLines[i - 1] });
      i--;
    }
  }
  entries.reverse();
  return entries;
}

export interface DiffChunk {
  id: number;
  oldLines: string[];
  newLines: string[];
  /** Index into the entries array where the chunk starts. */
  at: number;
  resolved: boolean;
  accepted: boolean;
}

/** Contiguous change blocks, each decided on its own. */
export function diffChunks(entries: DiffEntry[]): DiffChunk[] {
  const chunks: DiffChunk[] = [];
  let i = 0;
  while (i < entries.length) {
    if (entries[i].type === 'equal') {
      i++;
      continue;
    }
    const at = i;
    const oldLines: string[] = [], newLines: string[] = [];
    while (i < entries.length && entries[i].type !== 'equal') {
      if (entries[i].type === 'delete') oldLines.push(entries[i].line);
      else newLines.push(entries[i].line);
      i++;
    }
    chunks.push({ id: chunks.length, oldLines, newLines, at, resolved: false, accepted: false });
  }
  return chunks;
}

/** The text after applying the accepted chunks and keeping the old lines of the rejected ones. */
export function applyChunks(entries: DiffEntry[], chunks: DiffChunk[]): string {
  const decision = new Map<number, boolean>();
  for (const c of chunks) decision.set(c.at, c.accepted);
  const out: string[] = [];
  let i = 0;
  while (i < entries.length) {
    const e = entries[i];
    if (e.type === 'equal') {
      out.push(e.line);
      i++;
      continue;
    }
    const accepted = decision.get(i) ?? false;
    while (i < entries.length && entries[i].type !== 'equal') {
      const x = entries[i];
      if (accepted ? x.type === 'insert' : x.type === 'delete') out.push(x.line);
      i++;
    }
  }
  return out.join('\n');
}

export type WordPiece = { type: 'equal' | 'insert' | 'delete'; text: string };

/** Word-level diff of two short texts (a changed chunk), for the inline highlight. */
export function wordDiff(oldText: string, newText: string): WordPiece[] {
  const a = oldText.split(/(\s+)/).filter((x) => x !== '');
  const b = newText.split(/(\s+)/).filter((x) => x !== '');
  const m = a.length, n = b.length;
  if (m * n > 250_000) return [{ type: 'delete', text: oldText }, { type: 'insert', text: newText }];
  const dp: Uint16Array[] = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
  for (let i = 1; i <= m; i++) for (let j = 1; j <= n; j++) dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
  const out: WordPiece[] = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      out.push({ type: 'equal', text: a[i - 1] });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      out.push({ type: 'insert', text: b[j - 1] });
      j--;
    } else {
      out.push({ type: 'delete', text: a[i - 1] });
      i--;
    }
  }
  out.reverse();
  // Merge neighbours of the same type so the markup stays light.
  const merged: WordPiece[] = [];
  for (const p of out) {
    const last = merged[merged.length - 1];
    if (last && last.type === p.type) last.text += p.text;
    else merged.push({ ...p });
  }
  return merged;
}

/** A one-line summary of a diff: "+3 −1 lines". */
export function diffSummary(entries: DiffEntry[]): { added: number; removed: number } {
  let added = 0, removed = 0;
  for (const e of entries) {
    if (e.type === 'insert') added++;
    else if (e.type === 'delete') removed++;
  }
  return { added, removed };
}
