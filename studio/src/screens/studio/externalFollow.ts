/**
 * Generation gate for Studio's external-activity follow (#941).
 *
 * Idle→live and live→idle both kick off async history reads. Without a
 * token, a slow idle→live GET can finish after a later live→idle refresh and
 * overwrite the terminal transcript, then call resume on a finished turn.
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
