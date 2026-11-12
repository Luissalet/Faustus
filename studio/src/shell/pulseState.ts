import { sessionActivity, type ActivitySnapshot } from '../lib/activity';

export interface PulseSnapshot extends ActivitySnapshot {
  workers?: Record<string, unknown>;
  backgroundJobs?: { active: boolean }[];
  consoleWorkers?: number;
}

/** Waiting and queued work hold the light still; neither is inference. */
export function pulseView(snapshot: PulseSnapshot, openSessionId: string | null) {
  const local = sessionActivity(snapshot, openSessionId);
  const others = new Set([
    ...snapshot.running, ...snapshot.awaiting, ...Object.keys(snapshot.workers ?? {}),
    ...Object.keys(snapshot.queued).filter((id) => snapshot.queued[id] > 0),
  ]);
  if (openSessionId) others.delete(openSessionId);
  const background = others.size > 0 || !!snapshot.backgroundJobs?.some((job) => job.active)
    || (snapshot.consoleWorkers ?? 0) > 0;
  return {
    state: local === 'running' ? 'inferring' : background ? 'background' : 'idle',
    waiting: local === 'waiting' || local === 'queued',
  } as const;
}

/** A session query on another screen does not mean its chat is open. */
export function openSession(pathname: string, search: string): string | null {
  return pathname === '/' || pathname === '/studio' ? new URLSearchParams(search).get('s') || null : null;
}
