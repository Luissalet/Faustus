/**
 * Reading a run-activity snapshot: which conversation is doing what.
 *
 * Pure on purpose (`studio/checks/activity.check.mjs` runs it in node): the
 * polling and the React store live in `shell/activity.ts`, the rules about
 * what a dot means live here.
 */

export type SessionActivity = 'running' | 'queued' | 'waiting';

/** The shape `GET /api/chat/activity` answers with, as far as this cares. */
export interface ActivitySnapshot {
  /** Sessions with a run going (queued ones included). */
  running: string[];
  /** Sessions parked on an approval card nobody has answered. */
  awaiting: string[];
  /** Session → position in the queue while it waits for its lane. */
  queued: Record<string, number>;
}

/**
 * What that one session is doing, or null when it is doing nothing.
 *
 * "Waiting" outranks "running": a run parked on an approval card is still
 * registered as active, and what the person needs to see is that it is
 * waiting for THEM — that is the state that never ends on its own.
 */
export function sessionActivity(
  activity: ActivitySnapshot,
  sessionId: string | null | undefined,
): SessionActivity | null {
  if (!sessionId) return null;
  if (activity.awaiting.includes(sessionId)) return 'waiting';
  if (activity.queued[sessionId]) return 'queued';
  if (activity.running.includes(sessionId)) return 'running';
  return null;
}

/** The busiest state among several sessions, for a row that stands for many. */
export function groupActivity(activity: ActivitySnapshot, sessionIds: string[]): SessionActivity | null {
  let best: SessionActivity | null = null;
  for (const id of sessionIds) {
    const state = sessionActivity(activity, id);
    if (state === 'waiting') return 'waiting';
    if (state === 'running') best = 'running';
    else if (state === 'queued' && best === null) best = 'queued';
  }
  return best;
}
