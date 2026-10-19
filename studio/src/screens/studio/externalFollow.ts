/**
 * Generation gate and follow decisions for Studio's external-activity follow (#941).
 *
 * Idle→live and live→idle both kick off async history reads. Without a
 * token, a slow idle→live GET can finish after a later live→idle refresh and
 * overwrite the terminal transcript, then call resume on a finished turn.
 *
 * Binary live/idle alone also misses: a whole turn between idle polls, or a
 * new run id while still "running". Marks carry run id + session watermarks
 * from the native activity/session lists (no new Send/POST).
 */

export type ExternalFollowGate = {
  /** Bump and return the token for a new in-flight transition. */
  begin: () => number;
  /** True while no newer begin()/invalidate() has happened. */
  isCurrent: (token: number) => boolean;
  /** Drop every in-flight token (session switch, unmount). */
  invalidate: () => void;
  /** Current generation — tests only. */
  current: () => number;
};

export function createExternalFollowGate(start = 0): ExternalFollowGate {
  let gen = start;
  return {
    begin: () => {
      gen += 1;
      return gen;
    },
    isCurrent: (token) => token === gen,
    invalidate: () => {
      gen += 1;
    },
    current: () => gen,
  };
}

/** Shared predicate before applying a deferred history result. */
export function mayApplyExternalFollow(args: {
  token: number;
  gate: ExternalFollowGate;
  visibleSession: string | null | undefined;
  sid: string;
  epoch: number;
  currentEpoch: number;
  hasController: boolean;
}): boolean {
  if (!args.gate.isCurrent(args.token)) return false;
  if (args.visibleSession !== args.sid) return false;
  if (args.currentEpoch !== args.epoch) return false;
  if (args.hasController) return false;
  return true;
}

/** Native snapshot used to decide join vs history refresh. */
export type ExternalFollowMark = {
  live: boolean;
  runId: string;
  messageCount: number;
  lastMessageAt: string;
};

export type ExternalFollowAction = 'join' | 'refresh';

/**
 * Compare two marks. `undefined` prev = first sample after a session switch
 * (history-load owns that rejoin). Returns null when nothing to do.
 */
export function decideExternalFollowAction(
  prev: ExternalFollowMark | undefined,
  next: ExternalFollowMark,
): ExternalFollowAction | null {
  if (prev === undefined) return null;
  if (next.live && (!prev.live || (next.runId !== '' && next.runId !== prev.runId))) {
    return 'join';
  }
  if (prev.live && !next.live) return 'refresh';
  if (prev.runId !== '' && next.runId === '' && !next.live) return 'refresh';
  // Whole external turn between two idle polls: session list grew.
  // Require a prior stable watermark so the first sessions hydrate (0→N)
  // does not look like an external turn (history-load already owns open).
  const hadStable =
    prev.messageCount > 0 || prev.lastMessageAt !== '' || prev.live || prev.runId !== '';
  if (
    hadStable &&
    !prev.live &&
    !next.live &&
    (next.messageCount > prev.messageCount ||
      (next.lastMessageAt !== '' && next.lastMessageAt !== prev.lastMessageAt))
  ) {
    return 'refresh';
  }
  return null;
}

export type ExternalFollowPlan =
  | { kind: 'none'; nextPrev: ExternalFollowMark }
  | { kind: 'defer'; nextPrev: ExternalFollowMark | undefined; pending: ExternalFollowMark; action: ExternalFollowAction }
  | { kind: 'run'; nextPrev: ExternalFollowMark; action: ExternalFollowAction };

/**
 * Plan a follow transition without consuming `prev` while a legitimate
 * controller is still draining. Defer keeps the old→new RID edge intact so a
 * later flush (controller release / activity tick) can join once.
 */
export function planExternalFollowTransition(args: {
  prev: ExternalFollowMark | undefined;
  next: ExternalFollowMark;
  hasController: boolean;
}): ExternalFollowPlan {
  const action = decideExternalFollowAction(args.prev, args.next);
  if (!action) {
    return { kind: 'none', nextPrev: args.next };
  }
  if (args.hasController) {
    return { kind: 'defer', nextPrev: args.prev, pending: args.next, action };
  }
  return { kind: 'run', nextPrev: args.next, action };
}
