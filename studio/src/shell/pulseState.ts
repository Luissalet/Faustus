import { sessionActivity, type ActivitySnapshot } from '../lib/activity';

export interface PulseSnapshot extends ActivitySnapshot {
  workers?: Record<string, unknown>;
  details?: Record<string, { phase: string }>;
  backgroundJobs?: { active: boolean }[];
  activeAutomations?: number;
  agoraWorkers?: number;
}

export function pulseView(snapshot: PulseSnapshot, openSessionId: string | null) {
  const local = sessionActivity(snapshot, openSessionId);
  const phase = openSessionId ? snapshot.details?.[openSessionId]?.phase : undefined;
  const inferring = local === 'running' && (!phase || ['waiting_model', 'thinking', 'writing', 'finishing'].includes(phase));
  const background = snapshot.running.some((id) => id !== openSessionId && sessionActivity(snapshot, id) === 'running')
    || Object.keys(snapshot.workers ?? {}).some((id) => id !== openSessionId)
    || !!snapshot.backgroundJobs?.some((job) => job.active)
    || (snapshot.activeAutomations ?? 0) > 0 || (snapshot.agoraWorkers ?? 0) > 0;
  return {
    state: inferring ? 'inferring' : background ? 'background' : 'idle',
    waiting: local === 'waiting' || local === 'queued' || phase === 'awaiting_user',
  } as const;
}

/** A session query on another screen does not mean its chat is open. */
export function openSession(pathname: string, search: string): string | null {
  if (!['/', '/studio'].includes(pathname.replace(/\/$/, '') || '/')) return null;
  const params = new URLSearchParams(search);
  return params.get('agora') || params.get('s') || null;
}
