import { t } from '../i18n';
import { ApiError, asArray, getJson } from './api';

/**
 * Studio talks to the same chat backend the legacy screen does: one
 * POST to /api/chat_stream per turn, answered as server-sent events. This
 * file owns the wire format so the screen only ever sees typed events.
 *
 * Nothing here is new server surface. Sessions, history, models and the
 * stream are the endpoints static/js/chat.js already uses; the pilot must
 * be able to open a session the old UI created and vice versa.
 */

export interface ChatSession {
  id: string;
  name: string;
  model: string;
  endpointUrl: string;
  mode: 'chat' | 'agent' | null;
  messageCount: number;
  lastMessageAt: string | null;
  createdAt: string | null;
  folder: string | null;
  isImportant: boolean;
  hasDocuments: boolean;
  hasImages: boolean;
  totalTokens: number;
}

export interface ContextLedger {
  total: number;
  window: number;
  percent: number;
  sections: { label: string; tokens: number; percent: number }[];
  advice: { text: string; level: 'info' | 'warn' }[];
  /** Set when the server trimmed tool descriptions to make the window fit. */
  slim?: { before: number; after: number; limit: number };
}

export interface ModelRoute {
  /** Unique across endpoints: `${endpointId}::${model}`. */
  id: string;
  model: string;
  endpointId: string;
  endpointName: string;
  endpointUrl: string;
  kind: string;
}

export interface HistoryMessage {
  role: 'user' | 'assistant';
  content: string;
  metadata: Record<string, unknown>;
  /** Position in the server's history, which truncate counts from. */
  index: number;
}

export interface TurnMetrics {
  model?: string;
  responseTime?: number;
  outputTokens?: number;
  inputTokens?: number;
  tokensPerSecond?: number;
  contextPercent?: number;
}

export interface AskUser {
  question: string;
  options: string[];
  multi: boolean;
  kind: 'tool_approval' | 'question';
  approvalId?: string;
}

export interface WebSource {
  title: string;
  url: string;
}

export interface Todo {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  priority?: string;
  verified?: boolean;
}

export interface HarnessCheck {
  status: string;
  round?: number;
  reasons?: string[];
  label?: string;
  model?: string;
  detail?: string;
}

export interface HarnessSummary {
  toolCalls: number;
  failedCalls: number;
  mutations: string[];
  stopReason: string;
  notes: string[];
  checkpoint?: string;
  workspace?: string;
  tests?: Record<string, unknown>;
  review?: Record<string, unknown>;
  staticAnalysis?: Record<string, unknown>;
  changeset?: {
    verdict?: string;
    confidence?: number;
    unsupported: string[];
    unclaimed: string[];
    rendered?: string;
  };
}

/** A file write/edit as the server diffed it (also persisted in history). */
export interface StepDiff {
  text: string;
  file: string;
  added: number;
  removed: number;
  newFile: boolean;
}

/** One frame of what the agent sees: a browser page or the desktop. */
export interface BrowserFrame {
  src: string;
  url: string;
  title: string;
  tool: string;
  source: 'browser' | 'desktop';
  at: number;
}

/** A living document the agent created or changed (routes/document). */
export interface DocSnapshot {
  id: string;
  title: string;
  language: string;
  version: number;
  content: string;
}

export interface DocSuggestion {
  id: string;
  find: string;
  replace: string;
  reason: string;
}

/** The raw `subagent` payload of a tool_progress event (delegate_agents);
 *  every field optional, the reducer in screens/studio/model.ts folds it. */
export type SubagentPayload = Record<string, unknown>;

/** Everything the stream can say, narrowed to what the screen renders. */
export type ChatEvent =
  | { type: 'delta'; text: string; thinking: boolean }
  | { type: 'tool_start'; tool: string; command: string; fullCommand?: string; round: number }
  | { type: 'tool_progress'; tool: string; message: string }
  | {
      type: 'tool_output';
      tool: string;
      command: string;
      output: string;
      exitCode: number | null;
      diff?: StepDiff;
      docId?: string;
      /** A validated raster data: URL (desktop_screenshot and browser tools). */
      screenshot?: string;
    }
  | { type: 'subagent'; payload: SubagentPayload }
  | { type: 'frame'; frame: BrowserFrame }
  | { type: 'doc_open'; title: string; language: string }
  | { type: 'doc_delta'; content: string }
  | { type: 'doc_update'; doc: DocSnapshot }
  | { type: 'doc_suggestions'; docId: string; suggestions: DocSuggestion[] }
  | { type: 'round'; round: number }
  | { type: 'ask_user'; ask: AskUser }
  | { type: 'ask_resolved' }
  | { type: 'metrics'; metrics: TurnMetrics }
  | { type: 'sources'; sources: WebSource[] }
  | { type: 'research'; phase: string; round: number; totalSources: number; message: string; startedAt: number; avgDuration: number }
  | { type: 'image'; url: string }
  | { type: 'fallback'; answeredBy: string; selected: string }
  | { type: 'terminal'; failed: boolean; message?: string }
  | { type: 'error'; message: string }
  | { type: 'progress'; todos: Todo[] }
  | { type: 'plan'; plan: string }
  | { type: 'check'; check: HarnessCheck }
  | { type: 'summary'; summary: HarnessSummary }
  | { type: 'context'; percent?: number; tokens?: number; window?: number; ledger?: ContextLedger }
  | { type: 'done' };

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function num(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

export function metricsFrom(meta: Record<string, unknown>): TurnMetrics {
  return {
    model: str(meta.model) || undefined,
    responseTime: num(meta.response_time),
    outputTokens: num(meta.output_tokens),
    inputTokens: num(meta.input_tokens),
    tokensPerSecond: num(meta.tokens_per_second),
    contextPercent: num(meta.context_percent),
  };
}

/* ── Sessions ── */

interface RawSession {
  id: string;
  name?: string;
  model?: string;
  endpoint_url?: string;
  mode?: string | null;
  message_count?: number;
  last_message_at?: string | null;
  created_at?: string | null;
  folder?: string | null;
  is_important?: boolean;
  has_documents?: boolean;
  has_images?: boolean;
  total_tokens?: number;
}

export async function listSessions(signal?: AbortSignal): Promise<ChatSession[]> {
  const raw = asArray<RawSession>(await getJson<unknown>('/api/sessions', signal));
  return raw
    .map((s) => ({
      id: s.id,
      name: s.name?.trim() || t('Untitled'),
      model: s.model ?? '',
      endpointUrl: s.endpoint_url ?? '',
      mode: (s.mode === 'agent' || s.mode === 'chat' ? s.mode : null) as ChatSession['mode'],
      messageCount: s.message_count ?? 0,
      lastMessageAt: s.last_message_at ?? null,
      createdAt: s.created_at ?? null,
      folder: s.folder ?? null,
      isImportant: Boolean(s.is_important),
      hasDocuments: Boolean(s.has_documents),
      hasImages: Boolean(s.has_images),
      totalTokens: typeof s.total_tokens === 'number' ? s.total_tokens : 0,
    }))
    .sort((a, b) => (b.lastMessageAt ?? '').localeCompare(a.lastMessageAt ?? ''));
}

export async function loadHistory(
  sessionId: string,
  signal?: AbortSignal,
): Promise<{ name: string; model: string; history: HistoryMessage[] }> {
  const raw = await getJson<{ name?: string; model?: string; history?: unknown }>(
    `/api/history/${encodeURIComponent(sessionId)}`,
    signal,
  );
  const history = asArray<Partial<HistoryMessage>>(raw.history)
    .map((m, index) => ({ m, index }))
    .filter(({ m }) => m.role === 'user' || m.role === 'assistant')
    .map(({ m, index }) => ({
      role: m.role as 'user' | 'assistant',
      content: str(m.content),
      metadata: (m.metadata && typeof m.metadata === 'object' ? m.metadata : {}) as Record<
        string,
        unknown
      >,
      index,
    }));
  return { name: raw.name ?? '', model: raw.model ?? '', history };
}

/**
 * The legacy sidebar creates a session with a name, a route and
 * skip_validation so the server does not probe /v1/models on every new
 * chat. Same call, same fields (static/js/sessions.js).
 */
export async function createSession(
  name: string,
  route: ModelRoute | null,
): Promise<string> {
  const fd = new FormData();
  fd.append('name', name);
  fd.append('endpoint_url', route?.endpointUrl ?? '');
  fd.append('model', route?.model ?? '');
  fd.append('skip_validation', 'true');
  if (route?.endpointId) fd.append('endpoint_id', route.endpointId);
  const response = await fetch('/api/session', {
    method: 'POST',
    body: fd,
    credentials: 'same-origin',
  });
  if (!response.ok) throw new ApiError(`/api/session responded ${response.status}`, response.status);
  const payload = (await response.json()) as { id?: string };
  if (!payload.id) throw new ApiError('/api/session returned no id', 500);
  return payload.id;
}

/* ── Models ── */

interface RawModelItem {
  url?: string;
  endpoint_id?: string;
  endpoint_name?: string;
  endpoint_kind?: string;
  models?: string[];
  models_display?: string[];
  model_type?: string;
}

export async function listModels(signal?: AbortSignal, refresh = false): Promise<ModelRoute[]> {
  const raw = await getJson<{ items?: unknown }>(`/api/models?background=false${refresh ? '&refresh=true' : ''}`, signal);
  const routes: ModelRoute[] = [];
  for (const item of asArray<RawModelItem>(raw.items)) {
    if (item.model_type && item.model_type !== 'llm') continue;
    const endpointId = item.endpoint_id ?? '';
    for (const model of item.models ?? []) {
      routes.push({
        id: `${endpointId}::${model}`,
        model,
        endpointId,
        endpointName: item.endpoint_name ?? item.url ?? 'endpoint',
        endpointUrl: item.url ?? '',
        kind: item.endpoint_kind ?? 'remote',
      });
    }
  }
  return routes;
}

/* ── The stream ── */

export interface SendOptions {
  sessionId: string;
  message: string;
  mode: 'chat' | 'agent';
  planMode?: boolean;
  allowBash?: boolean;
  allowWebSearch?: boolean;
  useRag?: boolean;
  /** Deep Research before answering: several rounds of search and reading (`use_research`). */
  useResearch?: boolean;
  workspace?: string;
  route?: ModelRoute | null;
  /** Upload ids from /api/upload. */
  attachments?: string[];
  /** Per-session sampling knobs (/temp, /maxtokens…), validated server-side. */
  genOverrides?: Record<string, number | boolean>;
  /** Answering a tool approval: the message goes empty and these travel. */
  approval?: { id: string; decision: 'approve' | 'approve_task' | 'deny' };
  /** `/agents`: the delegation travels as its own field; the server swaps
   *  it in for the model and keeps `message` as the readable label. */
  delegateTasks?: Delegation;
  /** Nobody mode: nothing is persisted and memory tools stay closed. */
  incognito?: boolean;
  /** A preset id from /api/presets (system prompt + sampling). */
  presetId?: string;
  /** The document open in the panel, so the model sees what you see. */
  activeDocId?: string;
  /** Compare pane: no memory, no documents, only the tools the mode allows (`compare_mode`). */
  compare?: boolean;
  signal?: AbortSignal;
}

export interface DelegationTask {
  name: string;
  instruction: string;
  files?: string[];
  model?: string;
}

export interface Delegation {
  tasks: DelegationTask[];
  parallel: boolean;
  reviewer: boolean;
}

function timezoneHeaders(): Record<string, string> {
  let name = '';
  try {
    name = Intl.DateTimeFormat().resolvedOptions().timeZone ?? '';
  } catch {
    name = '';
  }
  return {
    'X-Tz-Offset': String(-new Date().getTimezoneOffset()),
    'X-Tz-Name': name,
  };
}

/** Only a raster data: URL may become an <img src>; the legacy renderer's
 *  safeToolScreenshotSrc applies the same rule (XSS through SVG/HTML). */
export function safeFrameSrc(raw: unknown): string {
  const src = String(raw ?? '').trim();
  return /^data:image\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(src) ? src : '';
}

function frameFrom(raw: Record<string, unknown>): BrowserFrame | null {
  const src = safeFrameSrc(raw.screenshot);
  if (!src) return null;
  const tool = str(raw.tool);
  const source = raw.source === 'desktop' || /^desktop_/.test(tool) ? 'desktop' : 'browser';
  return {
    src,
    url: str(raw.url).slice(0, 2048),
    title: (str(raw.title) || (source === 'desktop' ? t('Desktop') : '')).slice(0, 300),
    tool,
    source,
    at: Date.now(),
  };
}

export function diffFrom(raw: unknown): StepDiff | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const d = raw as Record<string, unknown>;
  const text = str(d.text);
  if (!text) return undefined;
  return {
    text,
    file: str(d.file, 'diff'),
    added: num(d.added) ?? 0,
    removed: num(d.removed) ?? 0,
    newFile: Boolean(d.new_file),
  };
}

/** `harness_summary` data, and the `harness` block history keeps. */
export function summaryFrom(data: Record<string, unknown>): HarnessSummary {
  const cs = (data.changeset && typeof data.changeset === 'object' ? data.changeset : null) as Record<string, unknown> | null;
  return {
    toolCalls: num(data.tool_calls) ?? 0,
    failedCalls: num(data.failed_calls) ?? 0,
    mutations: asArray<unknown>(data.mutations).map(String),
    stopReason: str(data.stop_reason, 'complete'),
    notes: asArray<unknown>(data.notes).map(String),
    checkpoint: str(data.checkpoint) || undefined,
    workspace: str(data.workspace) || undefined,
    tests: data.tests && typeof data.tests === 'object' ? (data.tests as Record<string, unknown>) : undefined,
    review: data.review && typeof data.review === 'object' ? (data.review as Record<string, unknown>) : undefined,
    staticAnalysis:
      data.static_analysis && typeof data.static_analysis === 'object' ? (data.static_analysis as Record<string, unknown>) : undefined,
    changeset: cs
      ? {
          verdict: str(cs.verdict) || undefined,
          confidence: num(cs.confidence),
          unsupported: asArray<Record<string, unknown>>(cs.unsupported_claims).map((p) => str(p.path)),
          unclaimed: asArray<unknown>(cs.unclaimed_changes).map((p) =>
            typeof p === 'string' ? p : str((p as Record<string, unknown>).path),
          ),
          rendered: str(cs.rendered) || undefined,
        }
      : undefined,
  };
}

/** A persisted tool call (`metadata.tool_events[i]` of an assistant message). */
export interface HistoryToolEvent {
  round: number;
  tool: string;
  command: string;
  output: string;
  exitCode: number | null;
  diff?: StepDiff;
  screenshot?: string;
  docId?: string;
  ask?: AskUser;
  askResolved: boolean;
  subagents: SubagentPayload[];
}

export function toolEventsFrom(meta: Record<string, unknown>): HistoryToolEvent[] {
  return asArray<Record<string, unknown>>(meta.tool_events).map((ev) => {
    const askRaw = (ev.ask_user && typeof ev.ask_user === 'object' ? ev.ask_user : null) as Record<string, unknown> | null;
    return {
      round: num(ev.round) ?? 1,
      tool: str(ev.tool, 'tool'),
      command: str(ev.command),
      output: str(ev.output),
      exitCode: num(ev.exit_code) ?? null,
      diff: diffFrom(ev.diff),
      screenshot: safeFrameSrc(ev.screenshot) || undefined,
      docId: str(ev.doc_id) || undefined,
      ask: askRaw
        ? {
            question: str(askRaw.question),
            options: asArray<unknown>(askRaw.options).map((o) =>
              typeof o === 'string' ? o : str((o as Record<string, unknown>).label ?? (o as Record<string, unknown>).value),
            ),
            multi: Boolean(askRaw.multi),
            kind: askRaw.kind === 'tool_approval' ? 'tool_approval' : 'question',
            approvalId: str(askRaw.approval_id) || undefined,
          }
        : undefined,
      askResolved: Boolean(askRaw?.resolved) || Boolean(ev.approved),
      subagents: asArray<SubagentPayload>(ev.subagents),
    };
  });
}

/** One raw `data:` payload → zero or one typed events. */
function decode(raw: Record<string, unknown>, sseEvent: string | null): ChatEvent | null {
  if (sseEvent === 'error') {
    // The server says `text`; older paths say `error` or `message`.
    return {
      type: 'error',
      message: str(raw.text ?? raw.error ?? raw.message ?? raw.detail, t('Server error')),
    };
  }
  if (typeof raw.delta === 'string') {
    return { type: 'delta', text: raw.delta, thinking: Boolean(raw.thinking) };
  }
  const data = (raw.data && typeof raw.data === 'object' ? raw.data : {}) as Record<string, unknown>;
  switch (raw.type) {
    case 'tool_start':
      return {
        type: 'tool_start',
        tool: str(raw.tool, 'tool'),
        command: str(raw.command),
        fullCommand: str(raw.full_command) || undefined,
        round: num(raw.round) ?? 1,
      };
    case 'tool_progress':
      // delegate_agents reports its workers through tool_progress with a
      // `subagent` payload (src/agent_tools/subagent_tools.py).
      if (raw.subagent && typeof raw.subagent === 'object') {
        return { type: 'subagent', payload: raw.subagent as SubagentPayload };
      }
      return {
        type: 'tool_progress',
        tool: str(raw.tool, 'tool'),
        message: str(raw.message ?? raw.event),
      };
    case 'tool_output':
      return {
        type: 'tool_output',
        tool: str(raw.tool, 'tool'),
        command: str(raw.command),
        output: str(raw.output),
        exitCode: num(raw.exit_code) ?? null,
        diff: diffFrom(raw.diff),
        docId: str(raw.doc_id) || undefined,
        screenshot: safeFrameSrc(raw.screenshot) || undefined,
      };
    case 'browser_view': {
      const frame = frameFrom(raw);
      return frame ? { type: 'frame', frame } : null;
    }
    case 'doc_stream_open':
      return { type: 'doc_open', title: str(raw.title), language: str(raw.language) };
    case 'doc_stream_delta':
      return { type: 'doc_delta', content: str(raw.content) };
    case 'doc_update':
      return raw.doc_id
        ? {
            type: 'doc_update',
            doc: {
              id: String(raw.doc_id),
              title: str(raw.title),
              language: str(raw.language),
              version: num(raw.version) ?? 1,
              content: str(raw.content),
            },
          }
        : null;
    case 'doc_suggestions':
      return {
        type: 'doc_suggestions',
        docId: str(raw.doc_id),
        suggestions: asArray<Record<string, unknown>>(raw.suggestions)
          .map((s) => ({ id: String(s.id ?? ''), find: str(s.find), replace: str(s.replace), reason: str(s.reason) }))
          .filter((s) => s.id && s.find),
      };
    case 'agent_step':
      return { type: 'round', round: num(raw.round) ?? 1 };
    // The server says the pending question has been answered — by this tab,
    // by another one, or by a continuation it is replaying. Either way the
    // card is stale and must go, or a second answer is sent for a decision
    // that has already been taken.
    case 'tool_approval_resolved':
    case 'ask_user_resolved':
      return { type: 'ask_resolved' };
    case 'ask_user':
      return {
        type: 'ask_user',
        ask: {
          question: str(data.question),
          options: asArray<string>(data.options).map(String),
          multi: Boolean(data.multi),
          kind: data.kind === 'tool_approval' ? 'tool_approval' : 'question',
          approvalId: str(data.approval_id) || undefined,
        },
      };
    case 'metrics':
      return { type: 'metrics', metrics: metricsFrom(data) };
    case 'web_sources':
    case 'research_sources':
      return {
        type: 'sources',
        sources: asArray<Record<string, unknown>>(raw.data)
          .map((s) => ({ title: str(s.title) || str(s.url), url: str(s.url) }))
          .filter((s) => s.url),
      };
    case 'research_progress':
      return {
        type: 'research',
        phase: str(data.phase),
        round: num(data.round) ?? 0,
        totalSources: num(data.total_sources) ?? 0,
        message: str(data.message),
        startedAt: num(data.started_at) ?? 0,
        avgDuration: num(data.avg_duration) ?? 0,
      };
    case 'generated_image':
      return raw.url ? { type: 'image', url: str(raw.url) } : null;
    case 'fallback':
      return { type: 'fallback', answeredBy: str(raw.answered_by), selected: str(raw.selected_model) };
    case 'agent_terminal': {
      const failure = (data.failure && typeof data.failure === 'object' ? data.failure : {}) as Record<
        string,
        unknown
      >;
      return { type: 'terminal', failed: true, message: str(failure.message) || undefined };
    }
    case 'chat_terminal':
      return { type: 'terminal', failed: false };
    case 'error':
      return { type: 'error', message: str(raw.text ?? raw.error ?? raw.message, t('Server error')) };
    case 'progress_update':
      return {
        type: 'progress',
        todos: asArray<Record<string, unknown>>(raw.todos).map((t) => ({
          content: str(t.content ?? t.text),
          status: (['pending', 'in_progress', 'completed'].includes(str(t.status)) ? str(t.status) : 'pending') as Todo['status'],
          priority: str(t.priority) || undefined,
          verified: typeof t.verified === 'boolean' ? t.verified : undefined,
        })),
      };
    case 'plan_update':
      return { type: 'plan', plan: str(data.plan) };
    case 'harness_check':
      return {
        type: 'check',
        check: {
          status: str(raw.status, 'unknown'),
          round: num(raw.round),
          reasons: asArray<unknown>(raw.reasons).map(String),
          label: str(raw.label) || undefined,
          model: str(raw.model) || undefined,
          detail: str(raw.detail ?? raw.reason ?? raw.message) || undefined,
        },
      };
    case 'harness_summary':
      return { type: 'summary', summary: summaryFrom(data) };
    case 'context_ledger': {
      // The percentage is the headline; the sections are why. "The model
      // ignored my instructions" is usually 9k of tool schemas and skills
      // spent before the question ever arrived.
      const sections = asArray<Record<string, unknown>>(data.sections).map((s) => ({
        label: str(s.label),
        tokens: num(s.tokens) ?? 0,
        percent: num(s.pct ?? s.percent) ?? 0,
      }));
      const advice = asArray<Record<string, unknown>>(data.advice).map((a) => ({
        text: str(a.text),
        level: (str(a.level) || 'info') as 'info' | 'warn',
      }));
      const slimRaw = (data.tool_slim ?? {}) as Record<string, unknown>;
      return {
        type: 'context',
        percent: num(data.percent ?? data.context_percent ?? data.used_percent),
        tokens: num(data.total ?? data.tokens ?? data.used_tokens),
        window: num(data.window ?? data.context_length),
        ledger: sections.length
          ? {
              total: num(data.total) ?? 0,
              window: num(data.window ?? data.context_length) ?? 0,
              percent: num(data.percent ?? data.context_pct ?? data.context_percent) ?? 0,
              sections,
              advice,
              slim: slimRaw.slimmed
                ? { before: num(slimRaw.before) ?? 0, after: num(slimRaw.after) ?? 0, limit: num(slimRaw.limit) ?? 0 }
                : undefined,
            }
          : undefined,
      };
    }
    default:
      return null;
  }
}

/**
 * Sends one turn and yields typed events until the server says [DONE].
 * The caller keeps an AbortController: aborting the fetch closes the
 * stream on our side, and `stopChat` tells the server to stop generating.
 */
export async function* sendTurn(options: SendOptions): AsyncGenerator<ChatEvent> {
  const fd = new FormData();
  fd.append('message', options.approval ? '' : options.message);
  fd.append('session', options.sessionId);
  fd.append('mode', options.mode);
  if (options.planMode) fd.append('plan_mode', 'true');
  if (options.allowBash) fd.append('allow_bash', 'true');
  if (options.allowWebSearch) fd.append('allow_web_search', 'true');
  if (options.useRag) fd.append('use_rag', 'true');
  if (options.useResearch) fd.append('use_research', 'true');
  if (options.workspace) fd.append('workspace', options.workspace);
  if (options.route) {
    fd.append('selected_model', options.route.model);
    if (options.route.endpointUrl) fd.append('selected_endpoint_url', options.route.endpointUrl);
    if (options.route.endpointId) fd.append('selected_endpoint_id', options.route.endpointId);
  }
  if (options.attachments?.length) fd.append('attachments', JSON.stringify(options.attachments));
  if (options.genOverrides && Object.keys(options.genOverrides).length) {
    fd.append('gen_overrides', JSON.stringify(options.genOverrides));
  }
  if (options.approval) {
    fd.append('tool_approval_id', options.approval.id);
    fd.append('tool_approval_decision', options.approval.decision);
  }
  if (options.delegateTasks) fd.append('delegate_tasks', JSON.stringify(options.delegateTasks));
  if (options.incognito) fd.append('incognito', 'true');
  if (options.presetId) fd.append('preset_id', options.presetId);
  if (options.activeDocId) fd.append('active_doc_id', options.activeDocId);
  if (options.compare) {
    fd.append('compare_mode', 'true');
    fd.append('no_documents', 'true');
    fd.append('no_memory', 'true');
  }

  const response = await fetch('/api/chat_stream', {
    method: 'POST',
    body: fd,
    headers: timezoneHeaders(),
    credentials: 'same-origin',
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    let detail = '';
    try {
      detail = str(((await response.json()) as { detail?: unknown }).detail);
    } catch {
      detail = '';
    }
    yield { type: 'error', message: detail || t('The server responded {status}', { status: response.status }) };
    yield { type: 'done' };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sseEvent: string | null = null;

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf('\n');
      while (newline !== -1) {
        const line = buffer.slice(0, newline).replace(/\r$/, '');
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf('\n');

        if (line.startsWith('event:')) {
          sseEvent = line.slice(6).trim();
          continue;
        }
        if (!line.startsWith('data:')) {
          if (line === '') sseEvent = null;
          continue;
        }
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') {
          yield { type: 'done' };
          return;
        }
        let raw: unknown;
        try {
          raw = JSON.parse(payload);
        } catch {
          continue;
        }
        if (raw && typeof raw === 'object') {
          const event = decode(raw as Record<string, unknown>, sseEvent);
          if (event) yield event;
        }
        sseEvent = null;
      }
    }
  } finally {
    reader.releaseLock();
  }
  yield { type: 'done' };
}

export async function stopChat(sessionId: string): Promise<void> {
  try {
    await fetch(`/api/chat/stop/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      credentials: 'same-origin',
    });
  } catch {
    /* the abort already closed our side; the server will notice */
  }
}
