import { ApiError, asArray, getJson } from './api';
import { t } from '../i18n';

/**
 * Agentes: the four panels the previous interface kept under the tools list
 * — Workers (`/api/dispatch`, workers.js), Agent runners
 * (`/api/agent-runners`, agentRunners.js), Agent definitions
 * (`/api/agent-defs`, agentDefs.js) and Expertos (`/api/experts`,
 * experts.js). Same routes, same bodies; this file only types them and keeps
 * the pure helpers those pages exported (task splitting, worker/delegate
 * status words, the review deltas) so the screen says the same things.
 */

async function ok(response: Response, what: string): Promise<Response> {
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown; error?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (body.detail != null) detail = JSON.stringify(body.detail);
      else if (typeof body.error === 'string') detail = body.error;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `${what} responded ${response.status}`, response.status);
  }
  return response;
}

async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  await ok(response, path);
  try {
    return (await response.json()) as T;
  } catch {
    return {} as T;
  }
}

const str = (v: unknown, fallback = ''): string => (v == null ? fallback : String(v));
const num = (v: unknown, fallback = 0): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
};
const list = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => String(x)) : []);

/* ── Workers (dispatch) ── */

export const LIVE_STATUSES = new Set(['queued', 'running', 'verifying', 'cancelling']);
export const isLive = (status: string): boolean => LIVE_STATUSES.has(status);

export interface JobTask {
  name?: string;
  instruction: string;
  files: string[];
  model?: string;
  runner?: string;
}

export interface WorkerProgress {
  last_event?: string;
  round?: number;
  last_tool?: string;
  tool?: string;
  elapsed_s?: number;
  stalled?: boolean;
  stall_reason?: string;
  state?: string;
  why?: string;
}

export interface WorkerReport {
  name: string;
  role: string;
  status: string;
  rounds: number;
  tool_calls: number;
  failed_calls: number;
  input_tokens: number;
  output_tokens: number;
  stop_reason: string;
  error: string;
  files_changed: string[];
  summary: string;
}

export interface Verification {
  ran: boolean;
  ok: boolean;
  inconclusive: boolean;
  summary: string;
  command: string;
  attempts: number;
  failures: string[];
  pre_existing: string[];
  output_tail: string;
  previous: { summary: string; failures: string[] }[];
}

export interface Proof {
  verdict: string;
  confidence: number | string;
  uncertainty: { kind: string; detail: string }[];
}

export interface JobChanges {
  added: string[];
  modified: string[];
  deleted: string[];
  source: string;
  truncated: boolean;
}

export interface JobResult {
  workers: WorkerReport[];
  files_changed: string[];
  changes: JobChanges | null;
  claimed_only: string[];
  verification: Verification | null;
  proof: Proof | null;
  totals: { errors: number };
  lock_conflicts: string[];
  dropped_tasks: number;
}

export interface DispatchJob {
  id: string;
  status: string;
  title: string;
  created: number;
  duration_s: number | null;
  session_id: string;
  error: string;
  verdict: string;
  phase: string;
  ceiling_s: number | null;
  progress: Record<string, WorkerProgress>;
  result: JobResult | null;
  tasks: JobTask[];
  workspace: string;
  model: string;
  verify: string;
}

export interface DispatchRequest {
  tasks: (string | { instruction: string; files?: string[]; runner?: string; name?: string })[];
  workspace: string;
  parallel?: boolean;
  reviewer?: boolean;
  fix_rounds?: number;
  verify?: string;
  model?: string;
  agent?: string;
  reviewer_agent?: string;
  runner?: string;
}

export interface DispatchConfig {
  model: string;
  server: string;
  error: string;
  verifier: { mode?: string; label?: string; kind?: string; error?: string } | null;
}

function verificationFrom(raw: unknown): Verification | null {
  if (!raw || typeof raw !== 'object') return null;
  const v = raw as Record<string, unknown>;
  return {
    ran: Boolean(v.ran),
    ok: Boolean(v.ok),
    inconclusive: Boolean(v.inconclusive),
    summary: str(v.summary),
    command: str(v.command),
    attempts: num(v.attempts, 0),
    failures: list(v.failures),
    pre_existing: list(v.pre_existing),
    output_tail: str(v.output_tail),
    previous: Array.isArray(v.previous)
      ? v.previous.map((p) => {
          const row = (p ?? {}) as Record<string, unknown>;
          return { summary: str(row.summary), failures: list(row.failures) };
        })
      : [],
  };
}

function resultFrom(raw: unknown): JobResult | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const changes = r.changes && typeof r.changes === 'object' ? (r.changes as Record<string, unknown>) : null;
  const proof = r.proof && typeof r.proof === 'object' ? (r.proof as Record<string, unknown>) : null;
  const totals = r.totals && typeof r.totals === 'object' ? (r.totals as Record<string, unknown>) : {};
  return {
    workers: asArray<Record<string, unknown>>(r.workers).map((w) => ({
      name: str(w.name, 'worker'),
      role: str(w.role, 'worker'),
      status: str(w.status),
      rounds: num(w.rounds),
      tool_calls: num(w.tool_calls),
      failed_calls: num(w.failed_calls),
      input_tokens: num(w.input_tokens),
      output_tokens: num(w.output_tokens),
      stop_reason: str(w.stop_reason),
      error: str(w.error),
      files_changed: list(w.files_changed),
      summary: str(w.summary),
    })),
    files_changed: list(r.files_changed),
    changes: changes
      ? { added: list(changes.added), modified: list(changes.modified), deleted: list(changes.deleted), source: str(changes.source), truncated: Boolean(changes.truncated) }
      : null,
    claimed_only: list(r.claimed_only),
    verification: verificationFrom(r.verification),
    proof: proof
      ? {
          verdict: str(proof.verdict),
          confidence: (proof.confidence as number | string) ?? '',
          uncertainty: asArray<Record<string, unknown>>(proof.uncertainty).map((u) => ({ kind: str(u.kind, '?'), detail: str(u.detail) })),
        }
      : null,
    totals: { errors: num(totals.errors) },
    lock_conflicts: list(r.lock_conflicts),
    dropped_tasks: num(r.dropped_tasks),
  };
}

export function jobFrom(raw: Record<string, unknown>): DispatchJob {
  return {
    id: str(raw.id),
    status: str(raw.status),
    title: str(raw.title),
    created: num(raw.created),
    duration_s: raw.duration_s == null ? null : num(raw.duration_s),
    session_id: str(raw.session_id),
    error: str(raw.error),
    verdict: str(raw.verdict),
    phase: str(raw.phase),
    ceiling_s: raw.ceiling_s == null ? null : num(raw.ceiling_s),
    progress: raw.progress && typeof raw.progress === 'object' ? (raw.progress as Record<string, WorkerProgress>) : {},
    result: resultFrom(raw.result),
    tasks: asArray<Record<string, unknown>>(raw.tasks).map((t) => ({
      name: t.name == null ? undefined : str(t.name),
      instruction: str(t.instruction),
      files: list(t.files),
      model: t.model == null ? undefined : str(t.model),
      runner: t.runner == null ? undefined : str(t.runner),
    })),
    workspace: str(raw.workspace),
    model: str(raw.model),
    verify: str(raw.verify),
  };
}

export async function listJobs(limit = 50): Promise<DispatchJob[]> {
  const data = await getJson<{ jobs?: unknown }>(`/api/dispatch?limit=${limit}`);
  return asArray<Record<string, unknown>>(data, 'jobs').map(jobFrom);
}

export async function getJob(id: string): Promise<DispatchJob> {
  return jobFrom(await getJson<Record<string, unknown>>(`/api/dispatch/${encodeURIComponent(id)}`));
}

export async function startJob(body: DispatchRequest): Promise<DispatchJob> {
  return jobFrom(await sendJson<Record<string, unknown>>('/api/dispatch', 'POST', body));
}

export async function cancelJob(id: string): Promise<void> {
  await sendJson(`/api/dispatch/${encodeURIComponent(id)}/cancel`, 'POST');
}

export async function dispatchConfig(workspace?: string): Promise<DispatchConfig> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : '';
  const raw = await getJson<Record<string, unknown>>(`/api/dispatch/config${q}`);
  return {
    model: str(raw.model),
    server: str(raw.server),
    error: str(raw.error),
    verifier: raw.verifier && typeof raw.verifier === 'object' ? (raw.verifier as DispatchConfig['verifier']) : null,
  };
}

/**
 * Follow one live job. Any failure (no EventSource, the SSE setting off —
 * the endpoint then answers JSON, which the stream rejects — a proxy) calls
 * `onFail` so the caller falls back to polling. Returns the close function.
 */
export function followJob(id: string, onEvent: () => void, onEnd: () => void, onFail: () => void): () => void {
  if (typeof EventSource === 'undefined') {
    onFail();
    return () => {};
  }
  let es: EventSource | null = null;
  try {
    es = new EventSource(`/api/dispatch/${encodeURIComponent(id)}/events?stream=1`);
  } catch {
    onFail();
    return () => {};
  }
  const close = () => {
    if (!es) return;
    try {
      es.close();
    } catch {
      /* already closed */
    }
    es = null;
  };
  es.onmessage = () => onEvent();
  es.addEventListener('end', () => {
    close();
    onEnd();
  });
  es.onerror = () => {
    close();
    onFail();
  };
  return close;
}

/**
 * The box's text as tasks: a blank line or a list marker (-, *, •, 1., 2))
 * starts a new one; a soft-wrapped paragraph stays ONE task. Max 4 — the
 * same rule as workers.js parseTasks, so a pasted paragraph never surprises.
 */
export function parseTasks(text: string): string[] {
  const marker = /^\s*(?:[-*•]|\d+[.)])\s+/;
  const tasks: string[] = [];
  let cur: string | null = null;
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) {
      if (cur) {
        tasks.push(cur);
        cur = null;
      }
      continue;
    }
    if (marker.test(raw)) {
      if (cur) tasks.push(cur);
      cur = line.replace(marker, '').trim();
    } else {
      cur = cur ? `${cur} ${line}` : line;
    }
  }
  if (cur) tasks.push(cur);
  return tasks.filter(Boolean).slice(0, 4);
}

export const PROOF_WORD: Record<string, string> = {
  proved: 'the tests passed and every claimed file really changed',
  partial: 'something is unaccounted for',
  unproved: 'nothing ran that could show it — not a failure, not a success',
  contradicted: 'the disk or the tests say otherwise',
};

export const PROOF_TONE: Record<string, 'ok' | 'warn' | 'bad'> = { proved: 'ok', partial: 'warn', unproved: 'warn', contradicted: 'bad' };

/* ── Agent runners ── */

export interface Runner {
  key: string;
  label: string;
  aliases: string[];
  kind: 'cli' | 'app';
  licence: 'open' | 'subscription' | 'unknown';
  install: string;
  launch_command: string;
  installed: boolean;
  path: string;
  version: string;
  invocation_known: boolean;
  runnable_as_worker: boolean;
  gate: string;
  gate_note: string;
  notes: string;
}

export interface RunnerCatalogue {
  runners: Runner[];
  enabled: boolean;
  guard_note: string;
  installed_count: number;
  runnable_count: number;
}

function runnerFrom(raw: Record<string, unknown>): Runner {
  const licence = str(raw.licence).trim().toLowerCase();
  const argv = Array.isArray(raw.argv) ? raw.argv : str(raw.argv) ? [raw.argv] : [];
  return {
    key: str(raw.key),
    label: str(raw.label) || str(raw.key),
    aliases: Array.isArray(raw.aliases)
      ? raw.aliases.map(String)
      : str(raw.aliases)
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
    kind: raw.kind === 'app' ? 'app' : 'cli',
    licence: licence === 'open' || licence === 'subscription' ? licence : 'unknown',
    install: str(raw.install),
    launch_command: str(raw.launch_command) || str(raw.install),
    installed: Boolean(raw.installed),
    path: str(raw.path),
    version: str(raw.version),
    invocation_known: raw.invocation_known === undefined ? argv.length > 0 : Boolean(raw.invocation_known),
    runnable_as_worker: Boolean(raw.runnable_as_worker),
    gate: str(raw.gate),
    gate_note: str(raw.gate_note),
    notes: str(raw.notes),
  };
}

export async function listRunners(refresh = false): Promise<RunnerCatalogue> {
  const raw = await getJson<Record<string, unknown>>(`/api/agent-runners${refresh ? '?refresh=1' : ''}`);
  const runners = asArray<Record<string, unknown>>(raw, 'runners').filter((r) => r && str(r.key)).map(runnerFrom);
  return {
    runners,
    enabled: raw.enabled !== false,
    guard_note: str(raw.guard_note),
    installed_count: num(raw.installed_count, runners.filter((r) => r.installed).length),
    runnable_count: num(raw.runnable_count, runners.filter((r) => r.runnable_as_worker).length),
  };
}

/** Why this agent can or cannot be a worker — the two facts kept apart. */
export function workerStatus(r: Runner): { can: boolean; label: string; detail: string } {
  if (r.kind === 'app') return { can: false, label: t('GUI, never a worker'), detail: t('A window that stays open has no one-task, one-exit invocation.') };
  if (!r.invocation_known) return { can: false, label: t('no invocation recorded'), detail: t('Ollama knows this agent; Faustus has no row saying how to run one task with it yet.') };
  if (!r.installed) return { can: false, label: t('not installed'), detail: t('Install it first: {cmd}', { cmd: r.install || r.launch_command }) };
  return { can: true, label: t('can be a worker'), detail: t('Put "runner": "{key}" on a dispatched task.', { key: r.key }) };
}

export interface LaunchEvent {
  event: string;
  command?: string;
  line?: string;
  message?: string;
  exit_code?: number | null;
  installed?: boolean;
}

export function launchLogLine(ev: LaunchEvent): string {
  if (ev.event === 'started') return `$ ${ev.command ?? ''}`;
  if (ev.event === 'output') return ev.line ?? '';
  if (ev.event === 'error') return `error: ${ev.message ?? ''}`;
  if (ev.event === 'end') {
    const code = ev.exit_code == null ? t('unknown') : String(ev.exit_code);
    return `— ${t('finished (exit {code})', { code })}; ${ev.installed ? t('it is now installed') : t('it is still not installed')}`;
  }
  return '';
}

/**
 * `ollama launch <key>`, its output as it arrives. This INSTALLS software,
 * so it is only ever a button the person pressed.
 */
export async function launchRunner(key: string, onLine: (line: string) => void, signal?: AbortSignal): Promise<void> {
  const response = await fetch(`/api/agent-runners/${encodeURIComponent(key)}/launch`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config_only: false }),
    signal,
  });
  await ok(response, 'launch');
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() || '';
    for (const frame of frames) {
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue;
        try {
          const text = launchLogLine(JSON.parse(line.slice(5).trim()) as LaunchEvent);
          if (text) onLine(text);
        } catch {
          /* not JSON */
        }
      }
    }
  }
}

/* ── Agent definitions ── */

export interface DefRule {
  effect: 'allow' | 'deny';
  what: string;
  detail: string;
}

export interface AgentDef {
  slug: string;
  name: string;
  description: string;
  mode: 'coordinator' | 'worker' | 'reviewer';
  source: 'builtin' | 'user' | 'repo';
  model: string;
  endpoint_id: string;
  runner: string;
  path: string;
  may_delegate: boolean;
  caveats: string[];
  rules: DefRule[];
  tools: string[];
  deny: string[];
  max_rounds: number | null;
}

export interface DefCatalogue {
  agents: AgentDef[];
  errors: { path: string; slug: string; reason: string }[];
  max_depth: number;
  depth_setting: string;
  shell_note: string;
}

function defFrom(raw: Record<string, unknown>): AgentDef {
  const mode = str(raw.mode);
  const source = str(raw.source);
  return {
    slug: str(raw.slug),
    name: str(raw.name) || str(raw.slug),
    description: str(raw.description),
    mode: mode === 'coordinator' || mode === 'reviewer' ? mode : 'worker',
    source: source === 'builtin' || source === 'repo' ? source : 'user',
    model: str(raw.model),
    endpoint_id: str(raw.endpoint_id),
    runner: str(raw.runner),
    path: str(raw.path),
    may_delegate: Boolean(raw.may_delegate),
    caveats: list(raw.caveats),
    rules: asArray<Record<string, unknown>>(raw.rules).map((r) => ({ effect: str(r.effect) === 'deny' ? 'deny' : 'allow', what: str(r.what), detail: str(r.detail) })),
    tools: list(raw.tools),
    deny: list(raw.deny),
    max_rounds: raw.max_rounds == null ? null : num(raw.max_rounds),
  };
}

/** The bound folder, so the repo's own definitions are asked for too. */
export function activeWorkspace(): string {
  try {
    return localStorage.getItem('odysseus-workspace') || '';
  } catch {
    return '';
  }
}

export async function listDefs(workspace = activeWorkspace()): Promise<DefCatalogue> {
  const raw = await getJson<Record<string, unknown>>(`/api/agent-defs${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''}`);
  return {
    agents: asArray<Record<string, unknown>>(raw, 'agents').filter((a) => a && str(a.slug)).map(defFrom),
    errors: asArray<Record<string, unknown>>(raw, 'errors').map((e) => ({ path: str(e.path), slug: str(e.slug), reason: str(e.reason, t('no reason given')) })),
    max_depth: num(raw.max_depth, 1),
    depth_setting: str(raw.depth_setting, 'agent_subagent_depth'),
    shell_note: str(raw.shell_note),
  };
}

export function delegateStatus(def: AgentDef, maxDepth: number): { can: boolean; label: string; detail: string } {
  if (!def.may_delegate) return { can: false, label: t('cannot delegate'), detail: t('It does one task and reports back.') };
  if (maxDepth < 1) return { can: false, label: t('cannot delegate'), detail: t('Asks to, and the depth ceiling is 0: no worker may start another.') };
  return { can: true, label: t('may delegate'), detail: t('Its own workers are the last generation (depth ceiling {n}).', { n: maxDepth }) };
}

export const MODE_HINT: Record<AgentDef['mode'], string> = {
  coordinator: 'May split its work between further workers, up to the depth ceiling.',
  reviewer: 'The only mode allowed to fill the reviewer slot, which runs after everyone with the file locks off.',
  worker: 'Does one task. Cannot start another worker.',
};

export const SOURCE_HINT: Record<AgentDef['source'], string> = {
  builtin: 'Shipped with Faustus. Put a file with the same slug under DATA_DIR/agents to replace it.',
  repo: 'Carried by this folder. It loaded because you approved this folder\'s instruction files.',
  user: 'Yours, under DATA_DIR/agents.',
};

/* ── Expertos ── */

export interface ExpertSummary {
  slug: string;
  name: string;
  description: string;
  model: string;
  enabled: boolean;
  owner: string;
  corpus_files: number;
  chunks: number;
  indexed_at: string | null;
  invocations: number;
  accepted: number;
  rejected: number;
  updated_at: string;
}

export interface ExpertProfile {
  slug: string;
  name: string;
  description: string;
  model: string;
  temperature: number;
  top_p: number;
  enabled: boolean;
  instructions: string;
  rubric: string[];
  owner: string;
  updated_at: string;
}

export interface CorpusFile {
  name: string;
  bytes: number;
  modified: number;
  pages: number | null;
  chunks: number;
  indexed_at: string | null;
}

export interface ExpertDetail {
  expert: ExpertProfile;
  usage: { invocations: number; accepted: number; rejected: number; last_used: string | null };
  files: CorpusFile[];
  chunks: number;
  indexed_at: string | null;
  collection: string;
}

export interface ExpertPatch {
  name?: string;
  description?: string;
  instructions?: string;
  rubric?: string[];
  model?: string;
  temperature?: number;
  top_p?: number;
  enabled?: boolean;
}

export interface SearchHit {
  chunk_id: string;
  source: string;
  page: number | null;
  start_line: number;
  end_line: number;
  score: number;
  text: string;
}

export interface CorpusSearch {
  query: string;
  tier: string;
  degraded: boolean;
  hits: SearchHit[];
}

export interface BlockPreview {
  text: string;
  chunk_ids: string[];
  chars: number;
  budget: number;
  degraded: boolean;
}

export interface ReindexResult {
  indexed: number;
  skipped: number;
  removed: number;
  chunks: number;
  seconds: number;
}

/** A rubric may arrive as a list or as the textarea's newline-separated text. */
export function rubricLines(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => str(item).trim()).filter(Boolean);
  return str(value)
    .split('\n')
    .map((line) => line.replace(/^\s*(?:[-*]|\d+[.)])\s+/, '').trim())
    .filter(Boolean);
}

const intOrNull = (v: unknown): number | null => {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
};

function summaryFrom(raw: Record<string, unknown>): ExpertSummary {
  return {
    slug: str(raw.slug),
    name: str(raw.name) || str(raw.slug),
    description: str(raw.description),
    model: str(raw.model),
    enabled: raw.enabled === undefined ? true : Boolean(raw.enabled),
    owner: str(raw.owner),
    corpus_files: num(raw.corpus_files),
    chunks: num(raw.chunks),
    indexed_at: raw.indexed_at ? str(raw.indexed_at) : null,
    invocations: num(raw.invocations),
    accepted: num(raw.accepted),
    rejected: num(raw.rejected),
    updated_at: str(raw.updated_at),
  };
}

function profileFrom(raw: Record<string, unknown>): ExpertProfile {
  const p = raw.expert && typeof raw.expert === 'object' ? (raw.expert as Record<string, unknown>) : raw;
  return {
    slug: str(p.slug),
    name: str(p.name) || str(p.slug),
    description: str(p.description),
    model: str(p.model),
    temperature: num(p.temperature, 0.2),
    top_p: num(p.top_p, 1),
    enabled: p.enabled === undefined ? true : Boolean(p.enabled),
    instructions: str(p.instructions),
    rubric: rubricLines(p.rubric),
    owner: str(p.owner),
    updated_at: str(p.updated_at),
  };
}

function detailFrom(raw: Record<string, unknown>): ExpertDetail {
  const usage = raw.usage && typeof raw.usage === 'object' ? (raw.usage as Record<string, unknown>) : {};
  return {
    expert: profileFrom(raw),
    usage: { invocations: num(usage.invocations), accepted: num(usage.accepted), rejected: num(usage.rejected), last_used: usage.last_used ? str(usage.last_used) : null },
    files: asArray<Record<string, unknown>>(raw, 'files')
      .filter((f) => f && f.name)
      .map((f) => ({ name: str(f.name), bytes: num(f.bytes), modified: num(f.modified), pages: intOrNull(f.pages), chunks: num(f.chunks), indexed_at: f.indexed_at ? str(f.indexed_at) : null })),
    chunks: num(raw.chunks),
    indexed_at: raw.indexed_at ? str(raw.indexed_at) : null,
    collection: str(raw.collection),
  };
}

export async function listExperts(): Promise<{ experts: ExpertSummary[]; enabled: boolean; context_chars: number }> {
  const raw = await getJson<Record<string, unknown>>('/api/experts');
  return {
    experts: asArray<Record<string, unknown>>(raw, 'experts').filter((e) => e && str(e.slug)).map(summaryFrom),
    enabled: raw.enabled !== false,
    context_chars: num(raw.context_chars),
  };
}

export async function createExpert(body: ExpertPatch & { name: string }): Promise<ExpertProfile> {
  return profileFrom(await sendJson<Record<string, unknown>>('/api/experts', 'POST', body));
}

export async function readExpert(slug: string): Promise<ExpertDetail> {
  return detailFrom(await getJson<Record<string, unknown>>(`/api/experts/${encodeURIComponent(slug)}`));
}

export async function updateExpert(slug: string, patch: ExpertPatch): Promise<ExpertProfile> {
  return profileFrom(await sendJson<Record<string, unknown>>(`/api/experts/${encodeURIComponent(slug)}`, 'PATCH', patch));
}

export async function deleteExpert(slug: string): Promise<void> {
  await sendJson(`/api/experts/${encodeURIComponent(slug)}`, 'DELETE');
}

export const corpusUrl = (slug: string, filename: string): string => `/api/experts/${encodeURIComponent(slug)}/corpus/${encodeURIComponent(filename)}`;

/** Multipart upload — the one call that must NOT send a JSON content type. */
export async function uploadCorpus(slug: string, files: FileList | File[]): Promise<{ uploaded: string[]; rejected: { name: string; reason: string }[] }> {
  const form = new FormData();
  for (const file of Array.from(files)) form.append('files', file, file.name);
  const response = await fetch(`/api/experts/${encodeURIComponent(slug)}/corpus`, { method: 'POST', credentials: 'same-origin', body: form });
  await ok(response, 'corpus');
  const raw = (await response.json()) as Record<string, unknown>;
  return {
    uploaded: asArray<unknown>(raw, 'uploaded').map((u) => (u && typeof u === 'object' ? str((u as Record<string, unknown>).name) : str(u))),
    rejected: asArray<Record<string, unknown>>(raw, 'rejected').map((r) => ({ name: str(r.name ?? r.file), reason: str(r.reason ?? r.error) })),
  };
}

export async function deleteCorpusFile(slug: string, name: string): Promise<void> {
  await sendJson(corpusUrl(slug, name), 'DELETE');
}

export async function reindexExpert(slug: string): Promise<ReindexResult> {
  const raw = await sendJson<Record<string, unknown>>(`/api/experts/${encodeURIComponent(slug)}/reindex`, 'POST');
  return { indexed: num(raw.indexed), skipped: num(raw.skipped), removed: num(raw.removed), chunks: num(raw.chunks), seconds: num(raw.seconds) };
}

export async function searchCorpus(slug: string, q: string): Promise<CorpusSearch> {
  const raw = await getJson<Record<string, unknown>>(`/api/experts/${encodeURIComponent(slug)}/search?q=${encodeURIComponent(q)}`);
  return {
    query: str(raw.query),
    tier: str(raw.tier, 'lexical'),
    degraded: Boolean(raw.degraded),
    hits: asArray<Record<string, unknown>>(raw, 'hits').map((h) => ({
      chunk_id: str(h.chunk_id),
      source: str(h.source),
      page: intOrNull(h.page),
      start_line: num(h.start_line),
      end_line: num(h.end_line),
      score: num(h.score),
      text: str(h.text),
    })),
  };
}

export async function previewBlock(slug: string, q: string): Promise<BlockPreview> {
  const raw = await getJson<Record<string, unknown>>(`/api/experts/${encodeURIComponent(slug)}/block?q=${encodeURIComponent(q)}`);
  const text = str(raw.text);
  return { text, chunk_ids: list(raw.chunk_ids), chars: num(raw.chars, text.length), budget: num(raw.budget), degraded: Boolean(raw.degraded) };
}

export async function sendFeedback(slug: string, accepted: number, rejected: number): Promise<void> {
  await sendJson(`/api/experts/${encodeURIComponent(slug)}/feedback?accepted=${accepted}&rejected=${rejected}`, 'POST');
}

/** "page 42" or "page unknown" — never a number that was not extracted. */
export function pageLabel(row: { page?: number | null; page_label?: string }): string {
  const given = str(row.page_label).trim();
  if (given) return given;
  return row.page == null ? t('page unknown') : t('page {n}', { n: row.page });
}

/* ── Expert review: typed span deltas over the ORIGINAL text ── */

export interface ReviewCitation {
  source: string;
  page: number | null;
  page_label?: string;
  ref?: string;
  marker?: string;
  known: boolean;
}

export interface ReviewDelta {
  id: string;
  op: string;
  span: { start: number | null; end: number | null };
  quote: string;
  replacement: string;
  rationale: string;
  rule: string;
  severity: 'low' | 'medium' | 'high';
  citations: ReviewCitation[];
  label: string;
  anchored: boolean;
  confidence: number;
  relocated: boolean;
  notes: string[];
}

export interface ReviewResult {
  expert: { slug: string; name: string; model: string };
  deltas: ReviewDelta[];
  rejected: { id: string; op: string; reason: string; quote: string }[];
  anchored_count: number;
  opinion_count: number;
  degraded: boolean;
  chunks: number;
  errors: unknown[];
  text: string;
}

function deltaFrom(raw: unknown): ReviewDelta {
  const d = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const span = (d.span && typeof d.span === 'object' ? d.span : {}) as Record<string, unknown>;
  const severity = str(d.severity).toLowerCase();
  return {
    id: str(d.id),
    op: str(d.op, 'EDIT').toUpperCase(),
    span: { start: intOrNull(span.start), end: intOrNull(span.end) },
    quote: str(d.quote),
    replacement: str(d.replacement),
    rationale: str(d.rationale),
    rule: str(d.rule),
    severity: severity === 'low' || severity === 'high' ? severity : 'medium',
    citations: asArray<Record<string, unknown>>(d.citations).map((c) => ({
      source: str(c.source),
      page: intOrNull(c.page),
      page_label: c.page_label == null ? undefined : str(c.page_label),
      ref: c.ref == null ? undefined : str(c.ref),
      marker: c.marker == null ? undefined : str(c.marker),
      known: c.known === undefined ? true : Boolean(c.known),
    })),
    label: str(d.label),
    anchored: Boolean(d.anchored),
    confidence: num(d.confidence),
    relocated: Boolean(d.relocated),
    notes: list(d.notes),
  };
}

/** The review result, bare or wrapped in {"result": …} / {"data": …}. */
export function reviewFrom(raw: unknown): ReviewResult {
  let data = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>;
  if (data.result && typeof data.result === 'object') data = data.result as Record<string, unknown>;
  else if (data.data && typeof data.data === 'object' && ((data.data as Record<string, unknown>).deltas || (data.data as Record<string, unknown>).expert)) data = data.data as Record<string, unknown>;
  const expert = (data.expert && typeof data.expert === 'object' ? data.expert : {}) as Record<string, unknown>;
  const deltas = asArray<unknown>(data.deltas).map(deltaFrom);
  const anchored = deltas.filter((d) => d.anchored).length;
  return {
    expert: { slug: str(expert.slug), name: str(expert.name) || str(expert.slug), model: str(expert.model) },
    deltas,
    rejected: asArray<Record<string, unknown>>(data.rejected).map((r) => {
      const inner = (r.raw && typeof r.raw === 'object' ? r.raw : {}) as Record<string, unknown>;
      return { id: str(r.id), op: str(r.op), reason: str(r.reason), quote: r.quote == null ? str(inner.quote) : str(r.quote) };
    }),
    anchored_count: data.anchored_count == null ? anchored : num(data.anchored_count, anchored),
    opinion_count: data.opinion_count == null ? deltas.length - anchored : num(data.opinion_count),
    degraded: Boolean(data.degraded),
    chunks: num(data.chunks),
    errors: Array.isArray(data.errors) ? data.errors : [],
    text: data.text == null ? str(data.original) : str(data.text),
  };
}

/**
 * Apply the accepted deltas to the original, RIGHT TO LEFT — the same rule
 * as src/expert_review.apply_deltas: every splice shifts the offsets after
 * it, so applying in reverse keeps the remaining spans valid.
 */
export function applyAcceptedDeltas(original: string, deltas: ReviewDelta[], acceptIds: Iterable<string> | null): string {
  let text = original;
  const wanted = acceptIds == null ? null : new Set(Array.from(acceptIds));
  const chosen: { start: number; end: number; replacement: string }[] = [];
  for (const d of deltas) {
    if (wanted !== null && !wanted.has(d.id)) continue;
    const { start, end } = d.span;
    if (start === null || end === null) continue;
    if (!(start >= 0 && start <= end && end <= text.length)) continue;
    chosen.push({ start, end, replacement: d.op === 'KILL' ? '' : d.replacement });
  }
  chosen.sort((a, b) => b.start - a.start || b.end - a.end);
  for (const d of chosen) text = text.slice(0, d.start) + d.replacement + text.slice(d.end);
  return text;
}

export function refOf(c: ReviewCitation): string {
  if (c.ref) return c.ref;
  return `${c.source || t('unknown source')}, ${pageLabel(c)}`;
}
