/**
 * BENCH-04 — a save that lost the save-vs-save race (`PUT /api/workspace/file`
 * answers 409 when the file's revision moved under the draft, per
 * `routes/workspace_routes.py::save_workspace_file`) needs a three-way view
 * before the person picks a resolution: what they started from, what they
 * typed, and what is on disk now. Pure and dependency-free (no diff library;
 * the repo takes on no new dependencies) — a line-by-line comparison is
 * enough to show where the two edits actually collide, distinct from lines
 * only one side touched.
 */

export interface ThreeWayLine {
  base: string | null;
  mine: string | null;
  theirs: string | null;
  /** 'same': neither side changed this line (or both made the identical
   *  edit). 'mine-only'/'theirs-only': only that side touched it — safe to
   *  keep. 'diverged': both sides changed it, and not to the same text —
   *  the only rows that actually need a person's judgment. */
  status: 'same' | 'mine-only' | 'theirs-only' | 'diverged';
}

/** Never drops a line silently: `mine`/`theirs` shorter than `base` show as
 *  `null` for the trailing rows, same as a line added past the other's end. */
export function threeWayLines(base: string, mine: string, theirs: string): ThreeWayLine[] {
  const b = base.split('\n');
  const m = mine.split('\n');
  const th = theirs.split('\n');
  const n = Math.max(b.length, m.length, th.length);
  const rows: ThreeWayLine[] = [];
  for (let i = 0; i < n; i++) {
    const bl = i < b.length ? b[i] : null;
    const ml = i < m.length ? m[i] : null;
    const tl = i < th.length ? th[i] : null;
    const mineChanged = ml !== bl;
    const theirsChanged = tl !== bl;
    let status: ThreeWayLine['status'];
    if (!mineChanged && !theirsChanged) status = 'same';
    else if (mineChanged && !theirsChanged) status = 'mine-only';
    else if (!mineChanged && theirsChanged) status = 'theirs-only';
    else status = ml === tl ? 'same' : 'diverged';
    rows.push({ base: bl, mine: ml, theirs: tl, status });
  }
  return rows;
}

export interface ThreeWaySummary { mineOnly: number; theirsOnly: number; diverged: number; total: number }

/** What the resolution banner leads with: how many lines actually collide,
 *  versus lines that are safe because only one side touched them. */
export function threeWaySummary(rows: ThreeWayLine[]): ThreeWaySummary {
  let mineOnly = 0, theirsOnly = 0, diverged = 0;
  for (const r of rows) {
    if (r.status === 'mine-only') mineOnly += 1;
    else if (r.status === 'theirs-only') theirsOnly += 1;
    else if (r.status === 'diverged') diverged += 1;
  }
  return { mineOnly, theirsOnly, diverged, total: rows.length };
}
