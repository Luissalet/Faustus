import type { RunStatus } from '../components';
import { ApiError, asArray, getJson } from './api';
import { createSession, listModels } from './chat';
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

export interface TaskRunDetail {
  taskId: string;
  taskType: string;
  action: string;
  result: string;
  error: string;
  model: string;
  endpointUrl: string;
  sessionId: string;
  researchId: string;
  outputTarget: string;
  tokens: number | null;
  rawStatus: string;
}

export interface ApprovalDetail {
  approvalId: string;
  action: string;
  detail: string;
  skillId: string;
  backend: string;
  recipients: string[];
  costUnits: number | null;
  secretNames: string[];
  permissions: Record<string, unknown>;
  outputKinds: string[];
  owner: string;
  expiresAt: string | null;
  usesLeft: number;
}

export interface RenderDetail {
  runId: string;
  recipe: string;
  rawStatus: string;
  record: Record<string, unknown>;
}

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
  /** How many identical rows this one stands for (the previous Activity stacked them). */
  repeats: number;
  task?: TaskRunDetail;
  approval?: ApprovalDetail;
  render?: RenderDetail;
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
export function normaliseStatus(raw: unknown): { status: RunStatus; label?: string } {
  const value = String(raw ?? '').toLowerCase();
  if (['success', 'succeeded', 'ok', 'done', 'completed'].includes(value)) return { status: 'succeeded' };
  if (['failed', 'error', 'failure'].includes(value)) return { status: 'failed' };
  if (['running', 'in_progress', 'started', 'active'].includes(value)) return { status: 'running' };
  if (['paused', 'suspended'].includes(value)) return { status: 'paused' };
  if (['cancelled', 'canceled', 'stopped', 'aborted', 'abort'].includes(value)) return { status: 'cancelled' };
  // A run that decided there was nothing to do: over, and not an error.
  if (value === 'skipped') return { status: 'cancelled', label: t('skipped') };
  if (['waiting', 'waiting_approval', 'pending_approval', 'needs_approval'].includes(value)) return { status: 'waiting' };
  if (['queued', 'pending', 'scheduled', ''].includes(value)) return { status: 'queued' };
  return { status: 'queued', label: value };
}

interface RawTaskRun {
  id: string;
  task_id?: string;
  task_name?: string;
  task_type?: string;
  action?: string;
  status?: string;
  result?: string | null;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  model?: string;
  endpoint_url?: string;
  session_id?: string;
  research_id?: string;
  output_target?: string;
  tokens_used?: number | null;
}

interface RawMediaRun {
  id?: string;
  run_id?: string;
  status?: string;
  recipe?: string;
  created_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

interface RawApproval {
  id?: string;
  approval_id?: string;
  status?: string;
  owner?: string;
  requested_at?: string | null;
  expires_at?: string | null;
  uses_left?: number;
  plan?: { action?: string; detail?: string; skill_id?: string; backend?: string; recipients?: string[]; cost_units?: number | null; secret_names?: string[]; permissions?: Record<string, unknown>; output_kinds?: string[] };
  action?: string;
  tool?: string;
}

const str = (v: unknown) => (typeof v === 'string' ? v : '');

function taskFrom(item: RawTaskRun): ActivityRun {
  const { status, label } = normaliseStatus(item.status);
  const result = str(item.result);
  const error = str(item.error);
  const placeholder = status === 'queued' ? t('Queued — waiting for a free slot…') : status === 'running' ? t('Running…') : '';
  return {
    id: item.id,
    kind: 'task',
    title: item.task_name || item.action?.replace(/_/g, ' ') || t('Task'),
    detail: (error || result || placeholder).slice(0, 220) || undefined,
    status,
    statusLabel: label,
    startedAt: item.started_at,
    finishedAt: item.finished_at,
    error: item.error,
    repeats: 1,
    task: {
      taskId: str(item.task_id),
      taskType: item.task_type || 'llm',
      action: str(item.action),
      result,
      error,
      model: str(item.model),
      endpointUrl: str(item.endpoint_url),
      sessionId: str(item.session_id),
      researchId: str(item.research_id),
      outputTarget: item.output_target || 'session',
      tokens: typeof item.tokens_used === 'number' ? item.tokens_used : null,
      rawStatus: str(item.status),
    },
  };
}

function approvalFrom(item: RawApproval, index: number): ActivityRun {
  const plan = item.plan ?? {};
  const action = plan.action || item.action || item.tool || '';
  const id = item.approval_id ?? item.id ?? `approval-${index}`;
  return {
    id,
    kind: 'approval',
    title: action ? action.replace(/_/g, ' ') : t('Action awaiting approval'),
    detail: plan.detail || undefined,
    status: 'waiting',
    startedAt: item.requested_at ?? null,
    repeats: 1,
    approval: {
      approvalId: id,
      action,
      detail: str(plan.detail),
      skillId: str(plan.skill_id),
      backend: str(plan.backend),
      recipients: Array.isArray(plan.recipients) ? plan.recipients.map(String) : [],
      costUnits: typeof plan.cost_units === 'number' ? plan.cost_units : null,
      secretNames: Array.isArray(plan.secret_names) ? plan.secret_names.map(String) : [],
      permissions: plan.permissions && typeof plan.permissions === 'object' ? plan.permissions : {},
      outputKinds: Array.isArray(plan.output_kinds) ? plan.output_kinds.map(String) : [],
      owner: str(item.owner),
      expiresAt: item.expires_at ?? null,
      usesLeft: typeof item.uses_left === 'number' ? item.uses_left : 1,
    },
  };
}

function renderFrom(item: RawMediaRun, index: number): ActivityRun {
  const { status, label } = normaliseStatus(item.status);
  const id = str(item.run_id) || str(item.id) || `media-${index}`;
  return {
    id,
    kind: 'render',
    title: item.recipe ? `${t('Render')} · ${item.recipe}` : t('Render'),
    detail: str(item.error) || undefined,
    status,
    statusLabel: label,
    startedAt: item.created_at,
    finishedAt: item.finished_at ?? null,
    error: item.error ?? null,
    repeats: 1,
    render: { runId: id, recipe: str(item.recipe), rawStatus: str(item.status), record: item as Record<string, unknown> },
  };
}

/**
 * Identical finished rows collapse into one with a count, the way the
 * previous Activity did — a mail task that says "no recent emails" every
 * two hours is one fact, not twelve rows.
 */
function stack(runs: ActivityRun[]): ActivityRun[] {
  const out: ActivityRun[] = [];
  const byKey = new Map<string, ActivityRun>();
  const hourBucket = (ts?: string | null) => {
    const d = ts ? new Date(ts) : null;
    if (!d || Number.isNaN(d.getTime())) return '';
    d.setMinutes(0, 0, 0);
    return d.toISOString();
  };
  for (const run of runs) {
    if (run.kind !== 'task' || !run.task) {
      out.push(run);
      continue;
    }
    const mail = /^Email\b/i.test(run.title);
    const text = run.task.result.trim();
    const normalised = mail ? (/^skipped\s*[—-]/i.test(text) || /\bNo recent emails\b/i.test(text) ? text.replace(/\d+/g, '#') : '__email_run__') : text;
    const key = [run.task.taskId, run.title, run.task.taskType, run.task.rawStatus, run.task.outputTarget, normalised, mail ? hourBucket(run.startedAt) : ''].join('');
    const existing = byKey.get(key);
    if (existing && run.status !== 'running' && run.status !== 'queued' && existing.repeats < 8) {
      existing.repeats += 1;
      continue;
    }
    byKey.set(key, run);
    out.push(run);
  }
  return out;
}

export async function loadActivity(signal?: AbortSignal): Promise<{ runs: ActivityRun[]; degraded: string[] }> {
  const degraded: string[] = [];
  const [tasks, media, approvals] = await Promise.all([
    getJson<unknown>('/api/tasks/runs/recent?limit=120', signal).catch(() => {
      degraded.push(t('task runs'));
      return { runs: [] };
    }),
    getJson<unknown>('/api/media/runs', signal).catch(() => {
      degraded.push(t('renders'));
      return { runs: [] };
    }),
    getJson<unknown>('/api/approvals/pending', signal).catch(() => {
      degraded.push(t('approvals'));
      return { pending: [] };
    }),
  ]);

  const runs: ActivityRun[] = [
    ...asArray<RawApproval>(approvals, 'pending').map(approvalFrom),
    ...stack(asArray<RawTaskRun>(tasks, 'runs').map(taskFrom)),
    ...asArray<RawMediaRun>(media, 'runs').map(renderFrom),
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

/* ── Decisions ── */

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

const post = (path: string, body?: unknown) =>
  fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });

/** The server records who decided; the reason is optional and kept. */
export async function decideApproval(approvalId: string, granted: boolean, reason = ''): Promise<void> {
  const r = await ok(await post(`/api/approvals/${encodeURIComponent(approvalId)}/${granted ? 'grant' : 'deny'}`, { reason }), 'approvals');
  const data = (await r.json()) as { ok?: boolean; detail?: string; reason?: string };
  if (data.ok === false) throw new ApiError(data.detail || data.reason || t('The decision was not recorded'), 409);
}

export async function cancelRender(runId: string): Promise<void> {
  await ok(await post(`/api/media/runs/${encodeURIComponent(runId)}/cancel`), 'media/cancel');
}

/**
 * "Open in chat": a new session seeded with the run's result, on the model
 * the task ran on when it is still reachable, else the default chat.
 */
export async function openRunInChat(run: ActivityRun): Promise<string> {
  const task = run.task;
  if (!task) throw new ApiError(t('Nothing to open'), 400);
  const routes = await listModels().catch(() => []);
  let route = task.model ? routes.find((r) => r.model === task.model) ?? null : null;
  if (!route && task.model && task.endpointUrl) route = { id: '', model: task.model, endpointId: '', endpointName: '', endpointUrl: task.endpointUrl, kind: '' };
  if (!route) {
    try {
      const dc = await getJson<{ endpoint_url?: string; model?: string; endpoint_id?: string }>('/api/default-chat');
      if (dc.endpoint_url) route = { id: '', model: dc.model ?? '', endpointId: dc.endpoint_id ?? '', endpointName: '', endpointUrl: dc.endpoint_url, kind: '' };
    } catch {
      /* fall through */
    }
  }
  if (!route) {
    const chatty = (m: string) => !['text-embedding', 'embedding', 'tts-', 'whisper', 'text-moderation', 'moderation-', 'dall-e', 'rerank'].some((p) => m.toLowerCase().includes(p));
    route = routes.find((r) => chatty(r.model)) ?? routes[0] ?? null;
  }
  const sid = await createSession(`${t('Task')}: ${run.title}`.slice(0, 60), route);
  await ok(
    await post(`/api/session/${encodeURIComponent(sid)}/inject_messages`, {
      messages: [
        { role: 'user', content: t('Here is the latest run of my scheduled task "{name}". Let\'s review it.', { name: run.title }) },
        { role: 'assistant', content: task.result || t('(no output)') },
      ],
    }),
    'session/inject',
  );
  return sid;
}

export function reportUrl(run: ActivityRun): string {
  return run.task?.researchId ? `/api/research/report/${encodeURIComponent(run.task.researchId)}` : '';
}
