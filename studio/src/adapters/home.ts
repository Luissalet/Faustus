import { asArray, getJson } from './api';

export interface ProjectSummary {
  id: string;
  name: string;
  folder?: string | null;
  workspace?: string | null;
  updated_at?: number | null;
}

export interface SessionSummary {
  id: string;
  name: string;
  model?: string | null;
  mode?: string | null;
  message_count?: number | null;
  updated_at?: string | null;
  last_message_at?: string | null;
}

export interface PendingApproval {
  id?: string;
  approval_id?: string;
  action?: string;
  tool?: string;
  session_id?: string;
  requested_at?: string;
}

export interface HomeData {
  projects: ProjectSummary[];
  sessions: SessionSummary[];
  approvals: PendingApproval[];
  /** Endpoints that failed, so the screen can be honest instead of empty. */
  degraded: string[];
}

/**
 * Everything Inicio needs, in one call each, tolerant of any single endpoint
 * being unavailable: a home screen that shows nothing because one fetch
 * failed is worse than one that says which part is missing.
 */
export async function loadHome(signal?: AbortSignal): Promise<HomeData> {
  const degraded: string[] = [];

  const [projects, sessions, approvals] = await Promise.all([
    getJson<unknown>('/api/projects', signal).catch(() => {
      degraded.push('proyectos');
      return [];
    }),
    getJson<unknown>('/api/sessions', signal).catch(() => {
      degraded.push('conversaciones');
      return [];
    }),
    getJson<unknown>('/api/approvals/pending', signal).catch(() => {
      degraded.push('aprobaciones');
      return { pending: [] };
    }),
  ]);

  return {
    projects: asArray<ProjectSummary>(projects, 'projects'),
    sessions: asArray<SessionSummary>(sessions, 'sessions'),
    approvals: asArray<PendingApproval>(approvals, 'pending'),
    degraded,
  };
}

/** Most recently touched first; the API's order is not guaranteed. */
export function byRecency<T extends { updated_at?: string | number | null; last_message_at?: string | null }>(
  items: T[],
): T[] {
  const stamp = (item: T): number => {
    const value = item.last_message_at ?? item.updated_at;
    if (typeof value === 'number') return value * 1000;
    if (typeof value === 'string') return Date.parse(value) || 0;
    return 0;
  };
  return [...items].sort((a, b) => stamp(b) - stamp(a));
}

export function relativeTime(value?: string | number | null): string {
  if (value === null || value === undefined) return '';
  const ms = typeof value === 'number' ? value * 1000 : Date.parse(value);
  if (!ms) return '';
  const minutes = Math.round((Date.now() - ms) / 60000);
  if (minutes < 1) return 'ahora';
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.round(hours / 24);
  if (days < 30) return `hace ${days} d`;
  const months = Math.round(days / 30);
  return `hace ${months} meses`;
}
