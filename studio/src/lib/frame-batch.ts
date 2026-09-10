/**
 * PERF-01/UX-05: coalesce rapid values into at most one delivery per
 * animation frame ("los deltas se aplican por requestAnimationFrame
 * agrupados").
 *
 * A local model can emit chunks far faster than the screen repaints (a
 * replayed buffer, or a fast backend on a short reply): applying every one
 * of them straight to React state means a full transcript re-render —
 * Markdown re-parsed, the tool rail re-laid-out — once per chunk instead of
 * once per frame, which is exactly the stretch where the composer stops
 * taking keystrokes. Coalescing here does not slow the model down or drop a
 * chunk's *content* (every `push` replaces the pending value; only the
 * *painted* cadence is capped), so the transcript still ends up textually
 * identical, just repainted at most 60 times a second instead of once per
 * chunk.
 *
 * The scheduler is injected (`requestAnimationFrame`/`cancelAnimationFrame`
 * by default) so this is testable head-on with a synchronous fake, the same
 * pattern `activity-poller.ts`'s `createActivityPoller` uses for its own
 * timers.
 */

export interface FrameScheduler {
  schedule: (cb: () => void) => number;
  cancel: (id: number) => void;
}

const domScheduler: FrameScheduler = {
  schedule: (cb) => window.requestAnimationFrame(cb),
  cancel: (id) => window.cancelAnimationFrame(id),
};

export interface FrameBatcher<T> {
  /** Replace the pending value; delivers on the next scheduled frame. */
  push: (value: T) => void;
  /** Deliver the pending value (if any) right now, skipping the wait. */
  flush: () => void;
  /** Drop whatever is pending; nothing more is delivered until the next `push`. */
  cancel: () => void;
}

/** `deliver` is called with the LAST value pushed before each frame — never
 *  once per `push`, and never more than once per frame. */
export function frameBatcher<T>(deliver: (value: T) => void, scheduler: FrameScheduler = domScheduler): FrameBatcher<T> {
  let pending: { value: T } | null = null;
  let frame: number | null = null;
  const run = () => {
    frame = null;
    if (!pending) return;
    const { value } = pending;
    pending = null;
    deliver(value);
  };
  return {
    push(value: T) {
      pending = { value };
      if (frame === null) frame = scheduler.schedule(run);
    },
    flush() {
      if (frame !== null) {
        scheduler.cancel(frame);
        frame = null;
      }
      run();
    },
    cancel() {
      if (frame !== null) {
        scheduler.cancel(frame);
        frame = null;
      }
      pending = null;
    },
  };
}
