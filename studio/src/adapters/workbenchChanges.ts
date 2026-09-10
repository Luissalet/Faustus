/**
 * BENCH-05 — "ChangeSet actual con diff por fichero" in the workbench's side
 * panel. Each tool call that wrote a file already carries its own diff
 * (`Step.diff`, `StepDiff` in `screens/studio/model.ts`, one per call); this
 * aggregates those into one row per file for the turn, so the panel shows
 * "this turn touched these N files" instead of one card per call — without
 * a second store: the same `Step.diff` the transcript's own tool cards
 * already render is the only source, just grouped differently here.
 *
 * Kept dependency-free from `screens/studio/model.ts` on purpose (duck-typed
 * input) so this stays testable with plain node, no JSX/React involved.
 */
export interface FileDiffLike {
  file: string;
  added: number;
  removed: number;
  newFile: boolean;
}

export interface FileChangeRow {
  file: string;
  added: number;
  removed: number;
  newFile: boolean;
  /** How many separate tool calls touched this file in the turn — a file
   *  edited three times in one turn is one row, not three. */
  edits: number;
}

export function aggregateFileChanges(diffs: FileDiffLike[]): FileChangeRow[] {
  const byFile = new Map<string, FileChangeRow>();
  for (const d of diffs) {
    if (!d.file) continue;
    const row = byFile.get(d.file);
    if (row) {
      row.added += d.added;
      row.removed += d.removed;
      row.newFile = row.newFile || d.newFile;
      row.edits += 1;
    } else {
      byFile.set(d.file, { file: d.file, added: d.added, removed: d.removed, newFile: d.newFile, edits: 1 });
    }
  }
  // Most-edited file first — the one worth looking at first in a turn that
  // touched several.
  return [...byFile.values()].sort((a, b) => (b.added + b.removed) - (a.added + a.removed));
}
