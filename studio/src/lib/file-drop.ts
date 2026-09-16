/**
 * File-drag highlight that survives the pointer crossing children of the
 * drop target. `dragleave` bubbles (unlike `mouseleave`), so a parent that
 * clears "dragging" on every leave flickers as the cursor moves from the
 * textarea onto a button — and Chrome/Electron can cancel the drop while
 * the target is being torn down.
 *
 * Enter/leave are counted, and a leave that would empty the count is
 * delayed so the matching enter of the next child (same turn or the next
 * frame) can cancel it. The highlight only clears when the pointer has
 * actually left the drop target.
 */

export function isFileDrag(types: ArrayLike<string> | null | undefined): boolean {
  if (!types) return false;
  return Array.from(types as ArrayLike<string>).includes('Files');
}

export type FileDropClock = {
  schedule: (fn: () => void, ms: number) => unknown;
  cancel: (id: unknown) => void;
};

const defaultClock: FileDropClock = {
  schedule: (fn, ms) => setTimeout(fn, ms),
  cancel: (id) => clearTimeout(id as ReturnType<typeof setTimeout>),
};

export const FILE_DROP_LEAVE_MS = 40;

export function createFileDropSession(
  onDragging: (active: boolean) => void,
  clock: FileDropClock = defaultClock,
  leaveDelay = FILE_DROP_LEAVE_MS,
) {
  let depth = 0;
  let leaveId: unknown = null;

  const clearLeave = () => {
    if (leaveId == null) return;
    clock.cancel(leaveId);
    leaveId = null;
  };

  return {
    enter(types: ArrayLike<string> | null | undefined) {
      if (!isFileDrag(types)) return;
      depth += 1;
      clearLeave();
      onDragging(true);
    },
    leave() {
      depth = Math.max(0, depth - 1);
      if (depth > 0) return;
      clearLeave();
      leaveId = clock.schedule(() => {
        leaveId = null;
        if (depth === 0) onDragging(false);
      }, leaveDelay);
    },
    drop() {
      depth = 0;
      clearLeave();
      onDragging(false);
    },
    dispose() {
      depth = 0;
      clearLeave();
    },
  };
}
