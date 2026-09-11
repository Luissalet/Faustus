import type { RunStatus } from '../components';
import { ApiError, asArray, getJson, responseReason } from './api';
import { chatActivity, createSession, listModels, listSessions, sendTurn, type AskOption, type ChatActivity, type ChatSession, type RunActivityDetail } from './chat';
import { sessionActivity } from '../lib/activity';
import { t } from '../i18n';
import type { AttentionRow } from './attention';

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

/** ACT-03: an `ask_user` question still waiting for an answer
 *  (`src/question_store.py`'s `list_open`, GET /api/questions) —
 *  the activity tray's own copy of what `AskUser` (chat.ts) carries on the
 *  live card, so "answer" here can drive the exact same path. */
export interface QuestionDetail {
  questionId: string;
  session: string;
  question: string;
  options: AskOption[];
  multi: boolean;
  expiresAt: string | null;
  revision: number;
}

export interface ActivityRun {
  id: string;
  kind: 'task' | 'render' | 'approval' | 'chat' | 'workflow' | 'question';
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
  /** Retained from a prior successful read, not a fresh server state. */
  stale?: boolean;
  task?: TaskRunDetail;
  approval?: ApprovalDetail;
  render?: RenderDetail;
  chat?: { sessionId: string; runId: string; model: string; progress?: RunActivityDetail };
  workflow?: WorkflowDetail;
  question?: QuestionDetail;
  /** ADP-11: present only for a `chat`/`question` row `mergeAttention` matched
   *  against `GET /api/attention` (or synthesized one for a finished, never-
   *  otherwise-listed conversation — see that function). */
  attention?: { kind: AttentionRow['kind']; reason: string; since: number | null; detail: string; unread: boolean; priority: number };
}

export interface WorkflowStep {
  id: string; title: string; status: string; reason: string; approvalId: string;
  wakeAt: string; needs: string[]; artifacts: ArtifactLink[];
}
export interface ArtifactLink { id: string; label: string; url: string }
export function artifactLinks(value: unknown): ArtifactLink[] {
  const seen = new Set<string>();
  return asArray<unknown>(value).slice(0, 100).flatMap((raw, index) => {
    const item = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
    const id = typeof raw === 'string' ? raw : str(item.id);
    if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id) || seen.has(id)) return [];
    seen.add(id);
    return [{ id, label: str(item.label).slice(0, 300) || t('Output {number}', { number: index + 1 }),
      url: `/api/artifacts/${encodeURIComponent(id)}/download` }];
  });
}
export interface WorkflowDetail { runId: string; recipe: string; projectId: string; nodes: WorkflowStep[] }

export function workflowFrom(item: Record<string, unknown>): ActivityRun {
  if (typeof item.id !== 'string' || !item.id) throw new ApiError(t('Invalid workflow response. Refresh to try again.'), 502);
  const nodes: WorkflowStep[] = asArray<Record<string, unknown>>(item.nodes).map((node) => ({
    id: str(node.id), title: str(node.title) || str(node.id), status: str(node.status),
    reason: str(node.reason), approvalId: str(node.approval_id), wakeAt: str(node.wake_at),
    needs: asArray<unknown>(node.needs).filter((v): v is string => typeof v === 'string'),
    artifacts: artifactLinks(node.artifacts),
  }));
  const mapped = normaliseStatus(item.status);
  const human = item.status === 'paused' && nodes.some((n) => n.status === 'paused' && n.approvalId);
  return { id: item.id, kind: 'workflow', title: str(item.title) || t('Workflow'),
    detail: str(item.reason), status: human ? 'waiting' : mapped.status, statusLabel: mapped.label,
    startedAt: str(item.started_at), finishedAt: str(item.finished_at), repeats: 1,
    workflow: { runId: item.id, recipe: str(item.workflow_id), projectId: str(item.project_id), nodes } };
}

/**
 * B2 (OBJ-8): the activity feed's own `WorkflowDetail` (above) is built for
 * the run-progress view — `workflowFrom` reads `nodes`/`status`, never the
 * definition itself, because `GET /api/workflows/runs` (the list) does not
 * carry it. The single-run route does: `GET /api/workflows/runs/{run_id}`
 * already returns `"definition": loaded["definition"].to_dict()`
 * (`routes/workflows_routes.py::get_run`) — an existing endpoint, not a new
 * one — so "Ver diagrama"/"Estimar coste" (Activity.tsx) fetch it here
 * before handing it to `adapters/topology.ts`'s `workflowMermaid`/
 * `workflowEstimate`, which both expect that same shape.
 */
export async function getWorkflowRunDefinition(runId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const body = await getJson<{ ok?: boolean; definition?: Record<string, unknown> }>(`/api/workflows/runs/${encodeURIComponent(runId)}`, signal);
  if (!body.definition || typeof body.definition !== 'object') {
    throw new ApiError(t('This run does not carry its definition — it may predate this build, or have been purged.'), 502);
  }
  return body.definition;
}

export async function changeWorkflow(runId: string, action: 'advance' | 'cancel', nodeId?: string): Promise<void> {
  const tail = nodeId ? `resume/${encodeURIComponent(nodeId)}` : action;
  const response = await fetch(`/api/workflows/runs/${encodeURIComponent(runId)}/${tail}`, {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ max_nodes: 10 }), signal: AbortSignal.timeout(20000),
  });
  await ok(response, 'workflows');
  const body = await response.json();
  if (body.ok !== true) throw new ApiError(body.reason || t('The workflow action was not confirmed. Refresh its status.'), 409);
}

/** Only live server-owned turns, never inferred from a conversation's age. */
export function conversationRuns(activity: ChatActivity, sessions: ChatSession[]): ActivityRun[] {
  const byId = new Map(sessions.map((s) => [s.id, s]));
  const ids = new Set([...activity.running, ...activity.awaiting, ...Object.keys(activity.queued)]);
  return [...ids].flatMap((id) => {
    const state = sessionActivity(activity, id);
    if (!state) return [];
    const session = byId.get(id);
    const progress = activity.details[id];
    const phase = progress?.phase;
    let detail = state === 'waiting' ? t('Waiting for your permission')
      : state === 'queued' ? t('Waiting for its turn (#{n})', { n: activity.queued[id] })
      : phase === 'tool' && progress?.tool ? t('Using {tool}', { tool: progress.tool.replace(/_/g, ' ') })
      : phase === 'thinking' ? t('Thinking')
      : phase === 'writing' ? t('Writing')
      : phase === 'waiting_model' ? t('Waiting for the model')
      : phase === 'research' ? t('Researching')
      : phase === 'starting' ? t('Starting') : t('Working now');
    if (state === 'running' && progress?.detail) detail += ` · ${progress.detail}`;
    return [{
      id, kind: 'chat' as const, title: session?.name || t('Conversation'),
      detail, status: state, repeats: 1,
      startedAt: progress?.startedAt && Number.isFinite(progress.startedAt)
        ? new Date(progress.startedAt).toISOString() : null,
      chat: { sessionId: id, runId: activity.runs[id] || progress?.runId || '',
              model: session?.model || '', progress },
    }];
  });
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
  if (value === 'submit_unknown') return { status: 'running', label: t('Checking submission') };
  if (value === 'unknown') return { status: 'running', label: t('Checking engine status') };
  if (['paused', 'suspended'].includes(value)) return { status: 'paused' };
  if (['cancelled', 'canceled', 'stopped', 'aborted', 'abort'].includes(value)) return { status: 'cancelled' };
  // A run that decided there was nothing to do: over, and not an error.
  if (value === 'skipped') return { status: 'cancelled', label: t('skipped') };
  if (['waiting', 'waiting_approval', 'pending_approval', 'needs_approval'].includes(value)) return { status: 'waiting' };
  if (['queued', 'pending', 'scheduled', 'submitted', 'submit_pending', ''].includes(value)) return { status: 'queued' };
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

interface RawQuestion {
  question_id?: string;
  session?: string;
  question?: string;
  options?: Array<{ label?: string; description?: string; id?: string }>;
  multi?: boolean;
  expires_at?: string | null;
  revision?: number;
  opened_at?: string | null;
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

function questionFrom(item: RawQuestion): ActivityRun {
  const id = str(item.question_id);
  const options: AskOption[] = asArray<{ label?: string; description?: string; id?: string }>(item.options)
    .map((o) => ({ label: str(o.label), description: str(o.description), id: str(o.id) || undefined }))
    .filter((o) => o.label);
  return {
    id,
    kind: 'question',
    title: str(item.question) || t('Question awaiting an answer'),
    status: 'waiting',
    startedAt: item.opened_at ?? null,
    repeats: 1,
    question: {
      questionId: id,
      session: str(item.session),
      question: str(item.question),
      options,
      multi: Boolean(item.multi),
      expiresAt: item.expires_at ?? null,
      revision: typeof item.revision === 'number' ? item.revision : 1,
    },
  };
}

export function renderFrom(item: RawMediaRun, index: number): ActivityRun {
  const { status, label } = normaliseStatus(item.status);
  const id = str(item.run_id) || str(item.id) || `media-${index}`;
  const recipe = str(item.workflow) || str(item.recipe);
  const reason = str(item.reason) || str(item.error);
  return {
    id,
    kind: 'render',
    title: recipe ? `${t('Render')} · ${recipe}` : t('Render'),
    detail: reason || undefined,
    status,
    statusLabel: label,
    startedAt: item.created_at,
    finishedAt: str(item.ended_at) || item.finished_at || null,
    error: status === 'failed' ? reason || null : null,
    repeats: 1,
    render: { runId: id, recipe, rawStatus: str(item.status), record: item as Record<string, unknown> },
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

export interface ActivityFeed {
  runs: ActivityRun[];
  degraded: string[];
  unavailableKinds: ActivityRun['kind'][];
}

export function retainUnavailableRuns(previous: ActivityRun[], feed: ActivityFeed): ActivityRun[] {
  return [...feed.runs, ...previous.filter((run) => feed.unavailableKinds.includes(run.kind))
    .map((run) => ({ ...run, stale: true }))];
}

export async function loadActivity(signal?: AbortSignal): Promise<ActivityFeed> {
  const degraded: string[] = [];
  const unavailableKinds: ActivityRun['kind'][] = [];
  const [tasks, media, approvals, questions, conversations, workflows] = await Promise.all([
    getJson<unknown>('/api/tasks/runs/recent?limit=120', signal).catch(() => {
      degraded.push(t('task runs'));
      unavailableKinds.push('task');
      return { runs: [] };
    }),
    getJson<unknown>('/api/media/runs', signal).catch(() => {
      degraded.push(t('renders'));
      unavailableKinds.push('render');
      return { runs: [] };
    }),
    getJson<unknown>('/api/approvals/pending', signal).catch(() => {
      degraded.push(t('approvals'));
      unavailableKinds.push('approval');
      return { pending: [] };
    }),
    getJson<unknown>('/api/questions', signal).catch(() => {
      degraded.push(t('questions'));
      unavailableKinds.push('question');
      return { questions: [] };
    }),
    chatActivity(signal).then(async (activity) => {
      if (!activity.running.length && !activity.awaiting.length && !Object.keys(activity.queued).length) return [];
      const sessions = await listSessions(signal).catch(() => {
        degraded.push(t('conversation names'));
        return [];
      });
      return conversationRuns(activity, sessions);
    }).catch(() => {
      degraded.push(t('conversations'));
      unavailableKinds.push('chat');
      return null;
    }),
    getJson<{ ok?: boolean; runs?: unknown }>('/api/workflows/runs?limit=80', signal).then((body) => {
      if (body.ok !== true || !Array.isArray(body.runs)) throw new ApiError('workflows', 502);
      return body.runs.map(workflowFrom);
    }).catch(() => {
      degraded.push(t('workflows'));
      unavailableKinds.push('workflow');
      return [];
    }),
  ]);
  signal?.throwIfAborted();
  // Empty and unavailable are different states. Keep the last good snapshot
  // in the screen when all sources are unavailable.
  if (unavailableKinds.length === 6) {
    throw new ApiError(t('Could not read the activity'), 503);
  }

  const runs: ActivityRun[] = [
    ...(conversations ?? []),
    ...workflows,
    ...asArray<RawApproval>(approvals, 'pending').map(approvalFrom),
    ...asArray<RawQuestion>(questions, 'questions').map(questionFrom),
    ...stack(asArray<RawTaskRun>(tasks, 'runs').map(taskFrom)),
    ...asArray<RawMediaRun>(media, 'runs').map(renderFrom),
  ];

  // Anything still waiting on a person goes first: an approval nobody is
  // shown is not a gate.
  const weight = (run: ActivityRun) => (run.status === 'waiting' ? 0 : run.status === 'running' ? 1 : run.status === 'queued' ? 2 : 3);
  runs.sort((a, b) => {
    const byState = weight(a) - weight(b);
    if (byState !== 0) return byState;
    return Date.parse(b.startedAt ?? '') - Date.parse(a.startedAt ?? '') || 0;
  });
  return { runs, degraded, unavailableKinds };
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
  fetch(path, { method: 'POST', credentials: 'same-origin', signal: AbortSignal.timeout(20000), headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });

/** The server records who decided; the reason is optional and kept. */
export async function decideApproval(approvalId: string, granted: boolean, reason = ''): Promise<void> {
  const r = await ok(await post(`/api/approvals/${encodeURIComponent(approvalId)}/${granted ? 'grant' : 'deny'}`, { reason }), 'approvals');
  const data = (await r.json()) as { ok?: boolean; detail?: string; reason?: string };
  if (data.ok === false) throw new ApiError(data.detail || data.reason || t('The decision was not recorded'), 409);
}

/**
 * ACT-03: answers an open question from the activity tray, through the
 * exact same path the live `AskCard` uses — `sendTurn` with `questionId`/
 * `optionIds` (Studio.tsx's `onAnswer`) — rather than a second resolution
 * route. `question_store.resolve()`'s dedupe/stale-revision/cancelled/
 * expired guards (CALL-07/TASK-04) apply identically either way, and a
 * rejection (409) surfaces as the same localized `ApiError` the card would
 * show. The tray does not render a transcript, so this drains the turn to
 * completion rather than yielding events; the caller re-fetches the feed
 * afterwards — the question simply stops being `open` once it is answered,
 * same as any other activity state change.
 */
export async function answerQuestion(question: QuestionDetail, text: string, optionIds?: string[]): Promise<void> {
  for await (const _event of sendTurn({
    sessionId: question.session,
    message: text,
    mode: 'agent',
    questionId: question.questionId,
    optionIds,
  })) {
    /* draining is the point: the server-side effect (the answer recorded,
       the turn resumed) has already happened by the time this yields. */
  }
}

export async function cancelRender(runId: string): Promise<void> {
  const response = await ok(await post(`/api/media/runs/${encodeURIComponent(runId)}/cancel`), 'media/cancel');
  const body = await response.json() as {ok?: boolean; detail?: string; reason?: string};
  if (body.ok !== true) throw new ApiError(body.detail || body.reason || t('The render could not be cancelled. Refresh and try again.'), 409);
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

/**
 * ACT-05: `GET /api/queue` (routes/queue_routes.py) — everything currently
 * queued or running across agent turns, background shell jobs, research and
 * media renders, in one owner-scoped list. A separate small feed from
 * `loadActivity` on purpose: those are finished-and-in-progress WORK items
 * (tasks/renders/approvals/conversations, each with its own rich detail
 * pane above); this is the narrower "what is waiting on what, and in which
 * order" queue view ACT-05 asks for, and reuses the run's own id — a queue
 * row for a `chat`/`render` activity row is the same run, not a new record.
 */
export interface QueueItem {
  kind: 'agent_run' | 'bg_job' | 'research' | 'media_run';
  id: string;
  sessionId: string;
  label: string;
  status: string;
  position: number | null;
  etaSeconds: number | null;
  startedAt: number | null;
  elapsedS: number | null;
  reorderable: boolean;
}

interface RawQueueItem {
  kind: QueueItem['kind'];
  id: string;
  session_id?: string;
  label?: string;
  status: string;
  position?: number | null;
  eta_seconds?: number | null;
  started_at?: number | null;
  elapsed_s?: number | null;
  reorderable?: boolean;
}

export async function loadQueue(signal?: AbortSignal): Promise<QueueItem[]> {
  const body = await getJson<{ ok: boolean; items: RawQueueItem[] }>('/api/queue', signal);
  return (body.items ?? []).map((item) => ({
    kind: item.kind,
    id: item.id,
    sessionId: item.session_id ?? '',
    label: item.label ?? item.id,
    status: item.status,
    position: item.position ?? null,
    etaSeconds: item.eta_seconds ?? null,
    startedAt: item.started_at ?? null,
    elapsedS: item.elapsed_s ?? null,
    reorderable: item.reorderable ?? false,
  }));
}

/**
 * Only `agent_run` items are actually reorderable (see routes/queue_routes.py's
 * module docstring for why the other three kinds have no ordered queue to
 * reorder); calling this on one of them surfaces the server's 409 as a
 * normal `ApiError` rather than pretending to have moved it.
 */
export async function prioritizeQueueItem(kind: QueueItem['kind'], id: string): Promise<void> {
  const response = await post(`/api/queue/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/priority`);
  if (!response.ok) {
    throw new ApiError(await responseReason(response, '/api/queue', t('Could not change this item’s priority')), response.status);
  }
}

/**
 * ADP-11: attach each `GET /api/attention` row to the run that already
 * represents its session — a `chat` row's `chat.sessionId`, or a `question`
 * row's `question.session` (both already come from `loadActivity`'s own
 * sources; this never asks the server a second time for what a run IS, only
 * for how urgently it needs a person).
 *
 * One case has nothing to attach to: `finished_unreviewed` is a session
 * whose run is no longer running/awaiting/queued, and `conversationRuns()`
 * only ever lists those three states (a finished chat simply is not in
 * `runs` today). For that kind only, a minimal `chat` row is synthesized
 * so the tray can show it at all — never for the other five kinds, which
 * by construction always have a live match already.
 */
export function mergeAttention(runs: ActivityRun[], rows: AttentionRow[]): ActivityRun[] {
  const bySession = new Map(rows.map((r) => [r.sessionId, r]));
  const matched = new Set<string>();
  const withAttention = runs.map((run) => {
    const sid = run.kind === 'chat' ? run.chat?.sessionId : run.kind === 'question' ? run.question?.session : undefined;
    const row = sid ? bySession.get(sid) : undefined;
    if (!sid || !row) return run;
    matched.add(sid);
    return { ...run, attention: { kind: row.kind, reason: row.reason, since: row.since, detail: row.detail, unread: row.unread, priority: row.priority } };
  });
  const synthesized: ActivityRun[] = [];
  for (const row of rows) {
    if (row.kind !== 'finished_unreviewed' || matched.has(row.sessionId)) continue;
    synthesized.push({
      id: row.sessionId,
      kind: 'chat',
      title: row.label || t('Conversation'),
      status: 'succeeded',
      repeats: 1,
      startedAt: row.since ? new Date(row.since * 1000).toISOString() : null,
      attention: { kind: row.kind, reason: row.reason, since: row.since, detail: row.detail, unread: row.unread, priority: row.priority },
      chat: { sessionId: row.sessionId, runId: '', model: '' },
    });
  }
  return [...withAttention, ...synthesized];
}
