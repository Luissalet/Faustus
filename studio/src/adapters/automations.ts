import { ApiError, asArray, getJson } from './api';
import { locale, t } from '../i18n';

const PERIOD_WORD: Record<string, string> = { day: 'day', iso_week: 'week', month: 'month' };

/** The scheduler's skip reasons are fixed English templates (src/task_scheduler.py,
 *  src/task_windows.py); shown in the interface language, anything else literal. */
export function runReasonText(reason: string | null | undefined): string {
  const text = String(reason ?? '');
  let m: RegExpMatchArray | null;
  if ((m = text.match(/^Due (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC; reached (\d+) min late, the window is (\d+) min$/))) {
    const when = new Date(`${m[1].replace(' ', 'T')}Z`);
    const shown = Number.isNaN(when.getTime()) ? `${m[1]} UTC` : when.toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' });
    return t('Due {when}; reached {late} min late, the window is {window} min', { when: shown, late: m[2], window: m[3] });
  }
  if ((m = text.match(/^Period (\S+) \((day|iso_week|month)\) already ran$/))) {
    return t('Period {period} ({kind}) already ran', { period: m[1], kind: t(PERIOD_WORD[m[2]] ?? m[2]) });
  }
  if (text === 'Task is paused; nothing ran') return t('Task is paused; nothing ran');
  if ((m = text.match(/^Paused; the (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC slot did not run$/))) {
    const when = new Date(`${m[1].replace(' ', 'T')}Z`);
    const shown = Number.isNaN(when.getTime()) ? `${m[1]} UTC` : when.toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' });
    return t('Paused; the {when} slot did not run', { when: shown });
  }
  return text;
}

/**
 * Automations (the previous interface's Tasks): `/api/tasks` and its
 * sub-routes, in the same shapes tasks.js used. A task is a recipe —
 * a trigger, what it does, where it delivers — plus the housekeeping
 * ones the server ships.
 */

export type TaskType = 'llm' | 'action' | 'research';
export type TriggerType = 'schedule' | 'event' | 'webhook';
export type Schedule = 'once' | 'daily' | 'weekly' | 'monthly' | 'cron';
/** At most one run per calendar period, in the task's own time zone. */
export type PeriodKey = 'day' | 'iso_week' | 'month';
/** How a run ends: the closed vocabulary the server reports for every run. */
export type ClosedStatus = 'ok' | 'partial' | 'failed' | 'skipped-window' | 'skipped-period' | 'skipped-paused' | 'blocked-login';

export interface Automation {
  id: string;
  name: string;
  task_type?: string | null;
  action?: string | null;
  prompt?: string | null;
  schedule?: string | null;
  cron_expression?: string | null;
  scheduled_time?: string | null;
  timezone?: string | null;
  scheduled_day?: number | null;
  scheduled_date?: string | null;
  trigger_type?: string | null;
  trigger_event?: string | null;
  trigger_count?: number | null;
  trigger_counter?: number | null;
  next_run?: string | null;
  last_run?: string | null;
  status?: string | null;
  run_count?: number | null;
  output_target?: string | null;
  session_id?: string | null;
  crew_member_id?: string | null;
  character_id?: string | null;
  model?: string | null;
  endpoint_url?: string | null;
  then_task_id?: string | null;
  notifications_enabled?: boolean;
  webhook_token?: string | null;
  /** CONTRATO_CONECTORES F2.2: connectors this scheduled task may use;
   *  `null`/absent inherits the project's (or every enabled connector). */
  connector_ids?: string[] | null;
  is_builtin?: boolean;
  is_modified?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  /** Minutes after its time a late or catch-up run is still allowed; absent: always. */
  window_minutes?: number | null;
  /** At most one run per day / ISO week / month; absent: no limit. */
  period_key?: PeriodKey | null;
  paused?: boolean;
  /** The newest run's closed status and its short reason (skips included). */
  last_run_closed_status?: ClosedStatus | null;
  last_run_reason?: string | null;
  last_run_at?: string | null;
}

export interface TaskRun {
  id: string;
  task_id: string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  result: string | null;
  error: string | null;
  tokens_used: number | null;
  model: string | null;
  /** Null while the run is still open (queued, running). */
  closed_status?: ClosedStatus | null;
  reason?: string;
}

export interface OutputTarget {
  value: string;
  label: string;
  description: string;
}

export interface ActionInfo {
  name: string;
  description: string;
}

export interface EventInfo {
  name: string;
  description: string;
}

/** What the form sends; the server fills the rest. */
export interface TaskInput {
  name?: string;
  prompt?: string;
  task_type?: TaskType;
  action?: string;
  schedule?: Schedule;
  scheduled_time?: string;
  timezone?: string;
  scheduled_day?: number;
  scheduled_date?: string;
  cron_expression?: string;
  trigger_type?: TriggerType;
  trigger_event?: string;
  trigger_count?: number;
  output_target?: string;
  model?: string;
  endpoint_url?: string;
  then_task_id?: string;
  notifications_enabled?: boolean;
  character_id?: string;
  connector_ids?: string[] | null;
  /** `null` clears the window / the period limit on an edit. */
  window_minutes?: number | null;
  period_key?: PeriodKey | null;
}

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown; error?: unknown; message?: unknown };
      for (const k of ['detail', 'error', 'message'] as const) if (typeof body[k] === 'string') { detail = body[k] as string; break; }
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

const post = (path: string, body?: unknown): Promise<Response> =>
  fetch(path, { method: 'POST', credentials: 'same-origin', headers: body === undefined ? undefined : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });

const enc = (id: string) => encodeURIComponent(id);

export interface EventJob {
  id: string; task_id: string; session_id: string | null; execution: string;
  delivery: string; delivery_error: string | null; result: string | null; model: string | null;
}
export interface AutomationEvent {
  id: string; source: string; event_type: string; event_key: string; jobs: EventJob[];
}
export function automationEvents(id: string): Promise<AutomationEvent[]> {
  return getJson<{ events: AutomationEvent[] }>(`/api/tasks/events?task_id=${enc(id)}`).then((r) => r.events);
}
export async function retryEventDelivery(id: string): Promise<void> {
  await ok(await post(`/api/tasks/events/${enc(id)}/retry-delivery`), 'events/retry-delivery');
}

/** The task routes report the connector selection as a nested
 *  `connectors: {connector_ids, effective, source}` block (F2); the
 *  screens read the flat `connector_ids`, so lift it here once. */
function withConnectorIds(a: Automation): Automation {
  if (a.connector_ids !== undefined) return a;
  const nested = (a as unknown as { connectors?: { connector_ids?: unknown } }).connectors;
  const ids = nested && Array.isArray(nested.connector_ids) ? nested.connector_ids.map(String) : null;
  return { ...a, connector_ids: ids };
}

export function listAutomations(signal?: AbortSignal): Promise<Automation[]> {
  return getJson<unknown>('/api/tasks', signal).then((value) => asArray<Automation>(value, 'tasks').map(withConnectorIds));
}

export async function createAutomation(input: TaskInput): Promise<Automation> {
  const r = await ok(await post('/api/tasks', input), 'tasks/create');
  const data = (await r.json()) as { task?: Automation } & Automation;
  return data.task ?? data;
}

export async function updateAutomation(id: string, input: TaskInput): Promise<void> {
  await ok(await fetch(`/api/tasks/${enc(id)}`, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) }), 'tasks/update');
}

export async function deleteAutomation(id: string): Promise<void> {
  await ok(await fetch(`/api/tasks/${enc(id)}`, { method: 'DELETE', credentials: 'same-origin' }), 'tasks/delete');
}

export async function pauseAutomation(id: string): Promise<void> {
  await ok(await post(`/api/tasks/${enc(id)}/pause`), 'tasks/pause');
}

export async function resumeAutomation(id: string): Promise<void> {
  await ok(await post(`/api/tasks/${enc(id)}/resume`), 'tasks/resume');
}

/** 409 means it is already running; `force` starts a second run beside it. */
export async function runAutomation(id: string, force = false): Promise<void> {
  const r = await post(`/api/tasks/${enc(id)}/run${force ? '?force=true' : ''}`);
  if (r.status === 409) throw new ApiError(t('It is already running'), 409);
  await ok(r, 'tasks/run');
}

export async function stopAutomation(id: string): Promise<void> {
  await ok(await post(`/api/tasks/${enc(id)}/stop`), 'tasks/stop');
}

export async function revertAutomation(id: string): Promise<void> {
  await ok(await post(`/api/tasks/${enc(id)}/revert`), 'tasks/revert');
}

/** Returns how many cached items went. */
export async function clearAutomationCache(id: string): Promise<number> {
  const r = await ok(await post(`/api/tasks/${enc(id)}/clear-cache`), 'tasks/clear-cache');
  const data = (await r.json()) as { cleared?: Record<string, number>; files?: number };
  return Object.values(data.cleared ?? {}).reduce((a, b) => a + Number(b || 0), 0) + Number(data.files || 0);
}

export async function regenerateWebhook(id: string): Promise<string> {
  const r = await ok(await post(`/api/tasks/${enc(id)}/webhook-regenerate`), 'tasks/webhook');
  const data = (await r.json()) as { webhook_token?: string; token?: string };
  return data.webhook_token ?? data.token ?? '';
}

export function webhookUrl(task: Automation): string {
  if (!task.webhook_token) return '';
  return `${window.location.origin}/api/tasks/${enc(task.id)}/webhook/${task.webhook_token}`;
}

export async function listRuns(id: string, limit = 20): Promise<TaskRun[]> {
  const data = await getJson<unknown>(`/api/tasks/${enc(id)}/runs?limit=${limit}`);
  return asArray<TaskRun>(data, 'runs');
}

export async function listOutputTargets(): Promise<OutputTarget[]> {
  try {
    return asArray<OutputTarget>(await getJson<unknown>('/api/tasks/meta/output-targets'), 'targets');
  } catch {
    return [{ value: 'session', label: 'Session', description: '' }];
  }
}

export async function listActions(): Promise<ActionInfo[]> {
  try {
    return asArray<ActionInfo>(await getJson<unknown>('/api/tasks/meta/actions'), 'actions');
  } catch {
    return [];
  }
}

export async function listEvents(): Promise<EventInfo[]> {
  try {
    return asArray<EventInfo>(await getJson<unknown>('/api/tasks/meta/events'), 'events');
  } catch {
    return [];
  }
}

/** The server drafts a task from a sentence; the person still reviews it. */
export async function draftFromText(description: string): Promise<TaskInput> {
  const r = await ok(await post('/api/tasks/parse', { description }), 'tasks/parse');
  const data = (await r.json()) as { success?: boolean; draft?: TaskInput; message?: string };
  if (!data.success || !data.draft) throw new ApiError(data.message || t('Could not draft it'), 422);
  return data.draft;
}

/** First open: the server creates its housekeeping tasks (paused) once. */
export async function ensureOnboarded(): Promise<void> {
  try {
    const state = await getJson<{ opened?: boolean }>('/api/tasks/onboarding');
    if (state.opened) return;
    await post('/api/tasks/onboarding', { enabled: false });
  } catch {
    /* the list still loads */
  }
}

/* ── Urgent-mail rules live with the account settings, like before ── */

export async function urgentEmailPrompt(): Promise<string> {
  try {
    const data = await getJson<{ urgent_email_prompt?: string }>('/api/auth/settings');
    return data.urgent_email_prompt ?? '';
  } catch {
    return '';
  }
}

export async function saveUrgentEmailPrompt(prompt: string): Promise<void> {
  await ok(await post('/api/auth/settings', { urgent_email_prompt: prompt }), 'auth/settings');
}

export interface EmailAccountLite {
  id: string;
  label: string;
  isDefault: boolean;
}

export async function emailAccountsForTasks(): Promise<EmailAccountLite[]> {
  try {
    const data = await getJson<unknown>('/api/email/accounts');
    return asArray<Record<string, unknown>>(data, 'accounts')
      .filter((a) => a.enabled !== false)
      .map((a) => ({ id: String(a.id ?? ''), label: String(a.name || a.email || a.address || a.id || ''), isDefault: Boolean(a.is_default) }))
      .filter((a) => a.id);
  } catch {
    return [];
  }
}

/* ── Run window, period limit and how a run ended ── */

export function closedLabel(status: string | null | undefined): string {
  switch (status) {
    case 'ok':
      return t('Completed');
    case 'partial':
      return t('Partial');
    case 'failed':
      return t('Failed');
    case 'skipped-window':
      return t('Skipped — outside its window');
    case 'skipped-period':
      return t('Skipped — period already ran');
    case 'skipped-paused':
      return t('Skipped — paused');
    case 'blocked-login':
      return t('Blocked — login');
    default:
      return String(status ?? '');
  }
}

/** The badge shape for a closed status; `null` for a run that is still open. */
export function closedBadge(status: string | null | undefined): 'succeeded' | 'failed' | 'waiting' | 'cancelled' | null {
  switch (status) {
    case 'ok':
      return 'succeeded';
    case 'partial':
      return 'waiting';
    case 'failed':
    case 'blocked-login':
      return 'failed';
    case 'skipped-window':
    case 'skipped-period':
    case 'skipped-paused':
      return 'cancelled';
    default:
      return null;
  }
}

export const PERIOD_KEYS: PeriodKey[] = ['day', 'iso_week', 'month'];

export function periodLabel(key: string | null | undefined): string {
  if (key === 'day') return t('At most once a day');
  if (key === 'iso_week') return t('At most once a week');
  if (key === 'month') return t('At most once a month');
  return '';
}

/** The task's run limits in words, or an empty list when it has none. */
export function describeGuards(task: Automation): string[] {
  const out: string[] = [];
  if (task.window_minutes) out.push(t('Only within {n} min of its time', { n: task.window_minutes }));
  if (task.period_key) out.push(periodLabel(task.period_key));
  return out;
}

/* ── The same vocabulary tasks.js kept ── */

export const EMAIL_ACCOUNT_ACTIONS = new Set(['summarize_emails', 'draft_email_replies', 'email_auto_translate', 'extract_email_events', 'check_email_urgency']);

export const CACHE_LABELS: Record<string, string> = {
  summarize_emails: 'email summaries',
  draft_email_replies: 'AI reply drafts',
  email_auto_translate: 'email translations',
  extract_email_events: 'email calendar cache',
  learn_sender_signatures: 'sender signatures',
  check_email_urgency: 'email tags',
};

const CATEGORY_MAP: Record<string, string> = {
  tidy_sessions: 'Chats',
  tidy_documents: 'Documents',
  consolidate_memory: 'Memory',
  tidy_research: 'Research',
  tidy_calendar: 'Calendar',
  classify_events: 'Calendar',
  ping_events: 'Calendar',
  extract_email_events: 'Calendar',
  summarize_emails: 'Email',
  draft_email_replies: 'Email',
  email_auto_translate: 'Email',
  learn_sender_signatures: 'Email',
  check_email_urgency: 'Email',
};

export function categoryOf(task: Automation): string {
  if (task.task_type === 'action' && task.action) return CATEGORY_MAP[task.action] ?? 'Other';
  if (task.task_type === 'llm' || !task.task_type) return task.crew_member_id ? 'Assistant' : 'Other';
  if (task.task_type === 'research') return 'Research';
  return 'Other';
}

export const PERSONAS: { value: string; label: string }[] = [
  { value: '', label: 'Default (no persona)' },
  { value: 'socrates', label: 'Socrates' },
  { value: 'razor', label: 'Razor' },
  { value: 'nietzsche', label: 'Nietzsche' },
  { value: 'spark', label: 'Spark' },
  { value: 'odysseus', label: 'Faustus' },
];

export interface Preset {
  label: string;
  desc: string;
  taskType: TaskType;
  triggerType: TriggerType;
}

export const PRESETS: Preset[] = [
  { label: 'Prompt on a schedule', desc: 'Run a prompt daily, weekly…', taskType: 'llm', triggerType: 'schedule' },
  { label: 'Prompt on an event', desc: 'Every N sessions or messages', taskType: 'llm', triggerType: 'event' },
  { label: 'Research on a schedule', desc: 'Deep research on a topic', taskType: 'research', triggerType: 'schedule' },
  { label: 'Research on an event', desc: 'Deep research after app events', taskType: 'research', triggerType: 'event' },
  { label: 'Action on a schedule', desc: 'Tidy or clean up on a timer', taskType: 'action', triggerType: 'schedule' },
  { label: 'Action on an event', desc: 'Tidy or clean up every N sessions or messages', taskType: 'action', triggerType: 'event' },
  { label: 'Webhook', desc: 'Fired by an external HTTP call', taskType: 'llm', triggerType: 'webhook' },
];

/* ── Email delivery target, encoded in one string like before ── */

export function parseEmailTarget(output: string | null | undefined): { enabled: boolean; to: string; accountId: string } {
  const raw = String(output ?? '').trim();
  if (!raw) return { enabled: false, to: '', accountId: '' };
  if (raw === 'email') return { enabled: true, to: '', accountId: '' };
  if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(raw)) return { enabled: true, to: raw, accountId: '' };
  if (!raw.startsWith('email:')) return { enabled: false, to: '', accountId: '' };
  let payload = raw.slice('email:'.length).trim();
  let accountId = '';
  const marker = '|account=';
  const i = payload.indexOf(marker);
  if (i >= 0) {
    accountId = payload.slice(i + marker.length).trim();
    payload = payload.slice(0, i).trim();
  }
  return { enabled: true, to: payload && payload !== 'self' ? payload : '', accountId };
}

export function buildEmailTarget(to: string, accountId: string): string {
  const cleanTo = to.trim();
  const cleanAccount = accountId.trim();
  const base = `email:${cleanTo || 'self'}`;
  return cleanAccount ? `${base}|account=${cleanAccount}` : cleanTo ? base : 'email';
}

/** Action tasks keep their small config in `prompt`: JSON or `key=value` lines. */
export function promptConfig(prompt: string | null | undefined): Record<string, string> {
  const raw = (prompt ?? '').trim();
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return Object.fromEntries(Object.entries(parsed as Record<string, unknown>).map(([k, v]) => [k, String(v ?? '')]));
  } catch {
    /* key=value lines */
  }
  const cfg: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    const i = line.indexOf('=');
    if (i <= 0) continue;
    cfg[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return cfg;
}

/* ── Times: the server keeps HH:MM in UTC, the person reads local ── */

export function utcToLocal(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number);
  if (!Number.isFinite(h)) return hhmm;
  const d = new Date();
  d.setUTCHours(h, m || 0, 0, 0);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function localToUtc(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number);
  if (!Number.isFinite(h)) return hhmm;
  const d = new Date();
  d.setHours(h, m || 0, 0, 0);
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
}

export const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

/** The common cron shapes in words; anything else stays as written. */
export function describeCron(expr: string): string {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return `cron ${expr}`;
  const [min, hour, dom, mon, dow] = parts;
  const pad = (v: string) => v.padStart(2, '0');
  const times = (h: string, m: string) => h.split(',').map((x) => `${pad(x)}:${pad(m)}`).join(', ');
  const everyN = /^\*\/(\d+)$/;
  if (dom === '*' && mon === '*' && dow === '*') {
    if (hour === '*' && min === '*') return t('Every minute');
    if (hour === '*' && everyN.test(min)) return t('Every {n} minutes', { n: min.match(everyN)![1] });
    if (hour === '*' && /^\d+$/.test(min)) return min === '0' ? t('Every hour') : t('Every hour at :{m}', { m: pad(min) });
    if (everyN.test(hour) && /^\d+$/.test(min)) {
      const n = hour.match(everyN)![1];
      return n === '1' ? t('Every hour') : t('Every {n} hours', { n });
    }
    if (/^[\d,]+$/.test(hour) && /^\d+$/.test(min)) return t('Every day at {time}', { time: times(hour, min) });
  }
  if (dom === '*' && mon === '*' && /^\d$/.test(dow) && /^[\d,]+$/.test(hour) && /^\d+$/.test(min)) {
    const day = DAYS[(Number(dow) + 6) % 7];
    return t('Every {day} at {time}', { day: t(day), time: times(hour, min) });
  }
  if (dom === '*' && mon === '*' && dow === '1-5' && /^[\d,]+$/.test(hour) && /^\d+$/.test(min)) return t('Weekdays at {time}', { time: times(hour, min) });
  return `cron ${expr}`;
}

/**
 * The recipe in one readable line.
 *
 * The product document is explicit that the primary view of an automation is
 * the sentence, not the node graph: "Cada lunes · Preparar resumen · Próxima
 * ejecución 09:00".
 */
export function describeTrigger(task: Automation): string {
  if (task.trigger_type === 'event' && task.trigger_event) {
    const times = task.trigger_count && task.trigger_count > 1 ? ` ×${task.trigger_count}` : '';
    return t('When {event} happens{times}', { event: task.trigger_event.replace(/_/g, ' '), times });
  }
  if (task.trigger_type === 'webhook') return t('When the webhook is called');
  if (task.schedule === 'cron' && task.cron_expression) return describeCron(task.cron_expression)+(task.timezone?` (${task.timezone})`:'');
  const time = task.scheduled_time ? task.timezone?`${task.scheduled_time} (${task.timezone})`:utcToLocal(task.scheduled_time) : '';
  if (task.schedule === 'daily') return t('Every day at {time}', { time });
  if (task.schedule === 'weekly') return t('Every {day} at {time}', { day: t(DAYS[task.scheduled_day ?? 0] ?? 'Monday'), time });
  if (task.schedule === 'monthly') return t('Day {n} of each month at {time}', { n: task.scheduled_day ?? 1, time });
  if (task.schedule === 'once') return t('Once, {date}', { date: task.scheduled_date ? new Date(task.scheduled_date).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : time });
  if (task.schedule && time) return t('{schedule} at {time}', { schedule: task.schedule, time });
  if (task.schedule) return String(task.schedule);
  return t('Manual only');
}

export function describeAction(task: Automation): string {
  if (task.task_type === 'action' && task.action) return task.action.replace(/_/g, ' ');
  if (task.task_type === 'research') return `${t('Research')}: ${(task.prompt ?? '').slice(0, 90)}`;
  if (task.prompt) return task.prompt.slice(0, 90);
  return task.task_type ?? t('action');
}

export function describeOutput(task: Automation): string {
  const raw = task.output_target ?? 'session';
  if (raw === 'none') return t('no delivery');
  if (raw === 'session' || raw === '') return t('to a chat session');
  if (raw === 'notification') return t('as a notification');
  const mail = parseEmailTarget(raw);
  if (mail.enabled) return mail.to ? t('by email to {to}', { to: mail.to }) : t('by email to me');
  return raw;
}
