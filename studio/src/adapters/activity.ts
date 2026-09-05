import type { RunStatus } from '../components';
import { asArray, getJson } from './api';
import { t } from '../i18n';

/**
 * One shape for every kind of work (UI-050).
 *
 * Faustus runs scheduled tasks, media renders, workflows, worker jobs and
 * agent turns, and each subsystem answers with its own vocabulary. Activity
 * normalises them **in the frontend first**, exactly as the plan says: no
 * backend migration is needed to stop the user having to learn five words
 * for "it failed".
 */
export interface ActivityRun {
  id: string;
  kind: 'task' | 'render' | 'approval';
  title: string;
  detail?: string;
  status: RunStatus;
  /** Present only when the subsystem used a word the map does not know. */
  statusLabel?: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  error?: string | null;
}

/**
 * Every status word the subsystems use, mapped to the seven the UI knows.
 *
 * `aborted` is here because the screen found it, not because anything
 * documented it: twenty of the twenty-three task runs on this machine use it
 * and were quietly rendering as "En cola". Which is the second half of this
 * function's job — an unknown word must NOT be dressed up as a known state.
 * It keeps the neutral shape and shows its own name, so the next vocabulary
 * nobody told us about is visible in one glance instead of being a lie.
 */
function normaliseStatus(raw: unknown): { status: RunStatus; label?: string } {
  const value = String(raw ?? '').toLowerCase();
  if (['success', 'succeeded', 'ok', 'done', 'completed'].includes(value))
    return { status: 'succeeded' };
  if (['failed', 'error', 'failure'].includes(value)) return { status: 'failed' };
  if (['running', 'in_progress', 'started', 'active'].includes(value))
    return { status: 'running' };
  if (['paused', 'suspended'].includes(value)) return { status: 'paused' };
  if (['cancelled', 'canceled', 'stopped', 'aborted', 'abort'].includes(value))
    return { status: 'cancelled' };
  if (['waiting', 'waiting_approval', 'pending_approval', 'needs_approval'].includes(value))
    return { status: 'waiting' };
  if (['queued', 'pending', 'scheduled', ''].includes(value)) return { status: 'queued' };
  return { status: 'queued', label: value };
}

interface TaskRun {
  id: string;
  task_name?: string;
  action?: string;
  status?: string;
  result?: string | null;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

interface MediaRun {
  id?: string;
  run_id?: string;
  status?: string;
  recipe?: string;
  created_at?: string | null;
}

interface PendingApproval {
  id?: string;
  approval_id?: string;
  action?: string;
  tool?: string;
  requested_at?: string | null;
}

export async function loadActivity(signal?: AbortSignal): Promise<{
  runs: ActivityRun[];
  degraded: string[];
}> {
  const degraded: string[] = [];

  const [tasks, media, approvals] = await Promise.all([
    getJson<unknown>('/api/tasks/runs/recent', signal).catch(() => {
      degraded.push(t('task runs'));
      return { runs: [] };
    }),
    getJson<unknown>('/api/media/runs', signal).catch(() => {
      degraded.push('renders');
      return { runs: [] };
    }),
    getJson<unknown>('/api/approvals/pending', signal).catch(() => {
      degraded.push('aprobaciones');
      return { pending: [] };
    }),
  ]);

  const runs: ActivityRun[] = [
    ...asArray<PendingApproval>(approvals, 'pending').map((item, index) => ({
      id: item.approval_id ?? item.id ?? `approval-${index}`,
      kind: 'approval' as const,
      title: item.action ?? item.tool ?? t('Action awaiting approval'),
      status: 'waiting' as RunStatus,
      startedAt: item.requested_at ?? null,
    })),
    ...asArray<TaskRun>(tasks, 'runs').map((item) => {
      const { status, label } = normaliseStatus(item.status);
      return {
        id: item.id,
        kind: 'task' as const,
        title: item.task_name || item.action || 'Tarea',
        detail: item.error || item.result || undefined,
        status,
        statusLabel: label,
        startedAt: item.started_at,
        finishedAt: item.finished_at,
        error: item.error,
      };
    }),
    ...asArray<MediaRun>(media, 'runs').map((item, index) => {
      const { status, label } = normaliseStatus(item.status);
      return {
        id: item.run_id ?? item.id ?? `media-${index}`,
        kind: 'render' as const,
        title: item.recipe ? `Render · ${item.recipe}` : 'Render',
        status,
        statusLabel: label,
        startedAt: item.created_at,
      };
    }),
  ];

  // Anything still waiting on a person goes first: an approval nobody is
  // shown is not a gate.
  const weight = (run: ActivityRun) => (run.status === 'waiting' ? 0 : run.status === 'running' ? 1 : 2);
  runs.sort((a, b) => {
    const byState = weight(a) - weight(b);
    if (byState !== 0) return byState;
    return Date.parse(b.startedAt ?? '') - Date.parse(a.startedAt ?? '') || 0;
  });

  return { runs, degraded };
}

export function duration(startedAt?: string | null, finishedAt?: string | null): string {
  if (!startedAt || !finishedAt) return '';
  const ms = Date.parse(finishedAt) - Date.parse(startedAt);
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60000)} min`;
}
