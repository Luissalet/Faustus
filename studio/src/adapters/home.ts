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

/**
 * The server writes naive ISO stamps (`2026-08-31T15:35:30.170267`) that
 * are UTC without saying so. Date.parse reads those as LOCAL time, which
 * made a session from two minutes ago say "hace 2 h" in Madrid. A stamp
 * with no zone is read as UTC; one that carries a zone is left alone.
 */
export function parseStamp(value: string): number {
  const naive = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(value);
  return Date.parse(naive ? `${value}Z` : value) || 0;
}

/** Most recently touched first; the API's order is not guaranteed. */
export function byRecency<T extends { updated_at?: string | number | null; last_message_at?: string | null }>(
  items: T[],
): T[] {
  const stamp = (item: T): number => {
    const value = item.last_message_at ?? item.updated_at;
    if (typeof value === 'number') return value * 1000;
    if (typeof value === 'string') return parseStamp(value);
    return 0;
  };
  return [...items].sort((a, b) => stamp(b) - stamp(a));
}

/**
 * Human time, in both directions.
 *
 * The first version only looked backwards, and an automation's next run is in
 * the future — Automatizaciones was reading "próxima hace 7 d", which is not
 * a sentence. Past gets "hace X", future gets "en X".
 */
export function relativeTime(value?: string | number | null): string {
  if (value === null || value === undefined) return '';
  const ms = typeof value === 'number' ? value * 1000 : parseStamp(value);
  if (!ms) return '';

  const deltaMinutes = Math.round((Date.now() - ms) / 60000);
  const past = deltaMinutes >= 0;
  const minutes = Math.abs(deltaMinutes);
  const say = (amount: string) => (past ? `hace ${amount}` : `en ${amount}`);

  if (minutes < 1) return 'ahora';
  if (minutes < 60) return say(`${minutes} min`);
  const hours = Math.round(minutes / 60);
  if (hours < 24) return say(`${hours} h`);
  const days = Math.round(hours / 24);
  if (days < 30) return say(`${days} d`);
  const months = Math.round(days / 30);
  return say(`${months} meses`);
}
