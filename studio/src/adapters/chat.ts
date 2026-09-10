import { t } from '../i18n';
import { ApiError, asArray, getJson, responseReason } from './api';
import { vramBlockedFrom, type VramBlocked } from './vramAdmission';

/**
 * What the model is doing while the turn waits for its first token, from
 * the heartbeat's `model_state` (the server asks Ollama /api/ps). "Waiting
 * for the model" with the model loaded read as a hang (Luis, 09-09-2026:
 * 35 GB in VRAM and PCIe spill at 01:28): the difference between loading
 * it, reading a long context, and spilling to RAM is the whole story.
 */
export function modelStateLabel(raw: unknown): string {
  if (!raw || typeof raw !== 'object') return '';
  const s = raw as Record<string, unknown>;
  if (s.resident === false) return t('Loading the model into memory');
  if (s.resident !== true) return '';
  const gb = (n: unknown) => (typeof n === 'number' && n > 0 ? (n / 1073741824).toFixed(1) : '');
  if (s.spill === true) {
    const inVram = gb(s.vram_bytes);
    const total = gb(s.size_bytes);
    return inVram && total
      ? t('The model is spilling to RAM ({v} of {s} GB in VRAM) — slow', { v: inVram, s: total })
      : t('The model is spilling to RAM — slow');
  }
  return t('The model is reading the context');
}

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
  /** `backend` = the model's own decode timings; `computed` = tokens over the
   *  turn's wall clock, which also divides by prefill and tool time. */
  tpsSource?: 'backend' | 'computed';
  contextPercent?: number;
}

export interface AskOption {
  label: string;
  description: string;
}

/**
 * Options arrive as `{label, description}` objects from the ask_user tool,
 * or as bare strings from older history. The live path used to `String()`
 * them, so the buttons read "[object Object]" (10-09-2026) — the card
 * "did not ask with options" because its options were unreadable.
 */
export function askOptionsFrom(raw: unknown): AskOption[] {
  return asArray<unknown>(raw)
    .map((o) => {
      if (typeof o === 'string') return { label: o.trim(), description: '' };
      if (o && typeof o === 'object') {
        const r = o as Record<string, unknown>;
        return { label: str(r.label ?? r.value ?? r.title).trim(), description: str(r.description).trim() };
      }
      return { label: '', description: '' };
    })
    .filter((o) => o.label);
}

export interface AskUser {
  question: string;
  options: AskOption[];
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
    id?: string;
    stored?: boolean;
    verified?: boolean;
    storageReason?: string;
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
  | { type: 'heartbeat'; phase: string; phaseAt: number; tool: string; detail: string; round: number }
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
  /** The VRAM admission gate before the turn's first call (OBJ-1): what it
   *  is doing, and the ticket to answer while `phase` is `vram_blocked`. */
  | { type: 'vram'; phase: string; message: string; blocked?: VramBlocked }
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
    tpsSource: meta.tps_source === 'backend' ? 'backend' : meta.tps_source === 'computed' ? 'computed' : undefined,
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

/** The header both `/api/chat_stream` and `/api/chat/resume` answer with,
 *  and the one `/api/chat/stop` demands before it cancels anything. */
export const RUN_ID_HEADER = 'X-Odysseus-Run-Id';

/** Set on a `/api/chat_stream` response that answered a duplicate
 *  `client_message_id` from the outbox instead of starting a new turn
 *  (UX-02/TASK-03) — a live reconnect or a replayed final state, never a
 *  fresh generation. */
export const IDEMPOTENT_REPLAY_HEADER = 'X-Faustus-Idempotent-Replay';

// ---------------------------------------------------------------------------
// client_message_id outbox (UX-02/TASK-03)
//
// A send is a promise the server hasn't answered yet: the fetch can be lost
// to a dropped connection, and a reload mid-send loses the in-memory guard
// that stops a double click. Both look identical from here — "I sent this
// and do not know whether it landed" — so both get the same answer: an entry
// in localStorage, keyed by session, written BEFORE the request goes out and
// cleared the moment a response comes back (the server received it, whatever
// the stream does after that). An entry still `sending` after a reload is
// exactly the case worth retrying, and it retries with the SAME id — never a
// new one, so the server's outbox (src/chat_outbox.py) recognises it as the
// same turn instead of starting a second one.
// ---------------------------------------------------------------------------

export interface OutboxEntry {
  id: string;
  text: string;
  /** Upload ids already on the server (`/api/upload`), not full Attachment
   *  objects — enough to resend the same turn, not to redraw its thumbnails. */
  attachments: string[];
  status: 'sending' | 'acked';
}

function outboxKey(sessionId: string): string {
  return `faustus_chat_outbox_${sessionId}`;
}

function readOutbox(sessionId: string): OutboxEntry | null {
  try {
    const raw = localStorage.getItem(outboxKey(sessionId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<OutboxEntry> | null;
    if (!parsed || typeof parsed.id !== 'string' || typeof parsed.text !== 'string') return null;
    return {
      id: parsed.id,
      text: parsed.text,
      attachments: Array.isArray(parsed.attachments) ? parsed.attachments.map(String) : [],
      status: parsed.status === 'acked' ? 'acked' : 'sending',
    };
  } catch {
    return null; // private mode, or corrupted JSON — treat as nothing pending
  }
}

function writeOutbox(sessionId: string, entry: OutboxEntry): void {
  try {
    localStorage.setItem(outboxKey(sessionId), JSON.stringify(entry));
  } catch {
    /* private mode: retry-on-reload becomes best-effort, not the send itself */
  }
}

/** Drop the session's outbox entry, if any — a response arrived (acked), or
 *  the send was cancelled on purpose (Stop), so there is nothing to retry. */
export function clearOutboxFor(sessionId: string): void {
  try {
    localStorage.removeItem(outboxKey(sessionId));
  } catch {
    /* private mode */
  }
}

/** An unacknowledged send left over from a reload or a dropped connection —
 *  the "still checking" case a screen retries with the SAME id, never a new
 *  one. `null` in the ordinary case: nothing pending. */
export function pendingOutboxFor(sessionId: string): OutboxEntry | null {
  const entry = readOutbox(sessionId);
  return entry && entry.status === 'sending' ? entry : null;
}

function newClientMessageId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  } catch {
    /* fall through to the timestamp form below */
  }
  return `cid_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

export interface SendOptions {
  sessionId: string;
  message: string;
  mode: 'chat' | 'agent';
  planMode?: boolean;
  allowBash?: boolean;
  allowWebSearch?: boolean;
  useRag?: boolean;
  /** Suppress automatic personal-memory retrieval without hiding project context. */
  noMemory?: boolean;
  noSkills?: boolean;
  inputTokenBudget?: number;
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
  /** The run's opaque id, as soon as the server answers: Stop needs it. */
  onRunId?: (runId: string | null) => void;
  signal?: AbortSignal;
  /** Reuse an existing id instead of minting one — retrying an
   *  unacknowledged send from `pendingOutboxFor` MUST pass its id back here;
   *  a fresh one would start a second turn instead of reconnecting to it. */
  clientMessageId?: string;
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
          id: typeof cs.id === 'string' ? cs.id : undefined,
          stored: typeof cs.stored === 'boolean' ? cs.stored : undefined,
          verified: cs.evidence_verified === true,
          storageReason: typeof cs.storage_reason === 'string' ? cs.storage_reason : undefined,
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
  /** The decision that closed the gate (`approve`, `approve_task`, `deny`). */
  askDecision?: string;
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
            options: askOptionsFrom(askRaw.options),
            multi: Boolean(askRaw.multi),
            kind: askRaw.kind === 'tool_approval' ? 'tool_approval' : 'question',
            approvalId: str(askRaw.approval_id) || undefined,
          }
        : undefined,
      askResolved: Boolean(askRaw?.resolved) || Boolean(ev.approved),
      askDecision: typeof askRaw?.resolved === 'string' ? askRaw.resolved : undefined,
      subagents: asArray<SubagentPayload>(ev.subagents),
    };
  });
}

/** One raw `data:` payload → zero or one typed events. */
export function decode(raw: Record<string, unknown>, sseEvent: string | null): ChatEvent | null {
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
          options: askOptionsFrom(data.options),
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
    case 'run_activity':
      return {
        type: 'heartbeat',
        phase: str(data.phase, 'waiting_model'),
        phaseAt: (num(data.phase_since) ?? 0) * 1000,
        tool: str(data.tool),
        detail: str(data.detail) || modelStateLabel(data.model_state),
        round: num(data.round) ?? 0,
      };
    case 'vram_admission': {
      const phase = str(data.phase);
      return {
        type: 'vram',
        phase,
        message: str(data.message),
        blocked: phase === 'vram_blocked' ? (vramBlockedFrom(data) ?? undefined) : undefined,
      };
    }
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
 * Reads one server-sent-event body and yields typed events until the server
 * says [DONE] (or the body ends).
 *
 * Shared by the POST that starts a turn and by the GET that reconnects to one
 * already running: a detached run replays its whole buffer to a late
 * subscriber, so the same decoder rebuilds the same turn either way.
 */
async function* streamEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
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

/**
 * Sends one turn and yields typed events until the server says [DONE].
 * The caller keeps an AbortController: aborting the fetch closes the
 * stream on our side, and `stopChat` tells the server to stop generating.
 */
export async function* sendTurn(options: SendOptions): AsyncGenerator<ChatEvent> {
  // Approvals, delegations and compare panes are not "a message the user
  // typed" in the sense a reload should retry: an approval is single-use at
  // the server regardless, a delegation has no plain-text form to resend,
  // and a compare pane is explicitly not resumable (routes/chat_routes.py —
  // "there's nothing to resume" for compare_mode). Only a plain send gets an
  // outbox entry and the id that comes with one.
  const trackOutbox = !options.approval && !options.delegateTasks && !options.compare;
  const clientMessageId = options.clientMessageId ?? (trackOutbox ? newClientMessageId() : '');
  if (trackOutbox && clientMessageId) {
    writeOutbox(options.sessionId, {
      id: clientMessageId,
      text: options.message,
      attachments: options.attachments ?? [],
      status: 'sending',
    });
  }

  const fd = new FormData();
  fd.append('message', options.approval ? '' : options.message);
  fd.append('session', options.sessionId);
  fd.append('mode', options.mode);
  if (clientMessageId) fd.append('client_message_id', clientMessageId);
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
  if (options.noMemory || options.incognito || options.compare) fd.set('no_memory', 'true');
  if (options.noSkills) fd.set('no_skills', 'true');
  if (options.inputTokenBudget != null) fd.set('input_token_budget', String(options.inputTokenBudget));
  if (options.presetId) fd.append('preset_id', options.presetId);
  if (options.activeDocId) fd.append('active_doc_id', options.activeDocId);
  if (options.compare) {
    fd.append('compare_mode', 'true');
    fd.append('no_documents', 'true');
  }

  let response: Response;
  try {
    response = await fetch('/api/chat_stream', {
      method: 'POST',
      body: fd,
      headers: timezoneHeaders(),
      credentials: 'same-origin',
      signal: options.signal,
    });
  } catch (error) {
    // A deliberate Stop (aborted on purpose) has nothing left to retry — but
    // any OTHER failure here means the browser never learned whether the
    // server saw this POST at all, which is exactly what the outbox entry
    // is for: it stays `sending` so a reload retries with this SAME id.
    if (trackOutbox && options.signal?.aborted) clearOutboxFor(options.sessionId);
    throw error;
  }
  // A response — any response — means the server received the send. What
  // happens to the STREAM from here is a separate question (resumeTurn's
  // job); the send itself is no longer in doubt, so the outbox entry is done.
  if (trackOutbox) clearOutboxFor(options.sessionId);

  if (!response.ok || !response.body) {
    yield { type: 'error', message: await responseReason(response, '/api/chat_stream') };
    yield { type: 'done' };
    return;
  }

  // The run's opaque identity. Stop is fail-closed on the server: without
  // this header back, `POST /api/chat/stop` refuses to cancel anything.
  options.onRunId?.(response.headers.get(RUN_ID_HEADER));
  yield* streamEvents(response.body);
}

/**
 * What the turn says when the wire broke, not the model.
 *
 * A server restart mid-turn (10-09-2026) ended the bubble with the browser's
 * own words - "Failed to fetch", "network error", "NetworkError when
 * attempting to fetch resource." - which name nothing the user can do. The
 * text that was streamed stays on screen; only the reason changes.
 */
export function streamFailureMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? '');
  const wire = error instanceof TypeError
    || /failed to fetch|network ?error|load failed|connection (was )?(reset|closed|refused|lost)|ERR_(CONNECTION|NETWORK|EMPTY)|socket hang up|aborted by the server/i.test(raw);
  if (wire) return t('The connection to the server dropped mid-turn. What arrived is kept; if the server restarted, send the message again.');
  return raw || t('The turn ended with an error.');
}

/**
 * Reconnects to a run that is still going server-side.
 *
 * A turn does not belong to the tab that started it: the server keeps the
 * run alive when the SSE client goes away (closed tab, a walk to another
 * screen) and replays its whole buffer to whoever subscribes next. Yields
 * nothing at all when there is no live run for the session — that is the
 * normal answer, not a failure.
 */
export async function* resumeTurn(
  sessionId: string,
  options: { signal?: AbortSignal; onRunId?: (runId: string | null) => void } = {},
): AsyncGenerator<ChatEvent> {
  let response: Response;
  try {
    response = await fetch(`/api/chat/resume/${encodeURIComponent(sessionId)}`, {
      credentials: 'same-origin',
      signal: options.signal,
    });
  } catch {
    return; // offline or aborted: the history already on screen stands
  }
  if (response.status === 404 || !response.ok || !response.body) return;
  options.onRunId?.(response.headers.get(RUN_ID_HEADER));
  yield* streamEvents(response.body);
}

/** What is alive right now, for the whole account, in one call. */
export interface RunActivityDetail {
  runId: string;
  phase: string;
  phaseSince: number;
  lastEventAt: number;
  serverAliveAt: number;
  startedAt: number;
  elapsedS: number;
  round: number;
  tool: string;
  detail: string;
  queuedPosition: number;
}

export interface ChatActivity {
  /** Sessions with a run going (queued ones included). */
  running: string[];
  /** Session → opaque run id, so a list can Stop what it shows. */
  runs: Record<string, string>;
  /** Session → current server-side phase of the detached run. */
  details: Record<string, RunActivityDetail>;
  /** Sessions parked on an approval card nobody has answered. */
  awaiting: string[];
  /** Session → position in the queue, while it waits for its lane. */
  queued: Record<string, number>;
}

export const EMPTY_ACTIVITY: ChatActivity = { running: [], runs: {}, details: {}, awaiting: [], queued: {} };

export async function chatActivity(signal?: AbortSignal): Promise<ChatActivity> {
  const raw = await getJson<{
    running?: unknown;
    runs?: unknown;
    details?: unknown;
    awaiting_approval?: unknown;
    queued?: unknown;
  }>('/api/chat/activity', signal);
  const ids = (value: unknown): string[] => asArray<unknown>(value).map(String).filter(Boolean);
  const map = <T>(value: unknown, cast: (v: unknown) => T): Record<string, T> => {
    const out: Record<string, T> = {};
    if (value && typeof value === 'object') {
      for (const [key, v] of Object.entries(value as Record<string, unknown>)) out[key] = cast(v);
    }
    return out;
  };
  const detailMap: Record<string, RunActivityDetail> = {};
  if (raw.details && typeof raw.details === 'object') {
    for (const [sessionId, value] of Object.entries(raw.details as Record<string, unknown>)) {
      if (!value || typeof value !== 'object') continue;
      const d = value as Record<string, unknown>;
      detailMap[sessionId] = {
        runId: str(d.run_id),
        phase: str(d.phase, 'starting'),
        phaseSince: (num(d.phase_since) ?? 0) * 1000,
        lastEventAt: (num(d.last_event_at) ?? 0) * 1000,
        serverAliveAt: (num(d.server_alive_at) ?? 0) * 1000,
        startedAt: (num(d.started_at) ?? 0) * 1000,
        elapsedS: num(d.elapsed_s) ?? 0,
        round: num(d.round) ?? 0,
        tool: str(d.tool),
        detail: str(d.detail),
        queuedPosition: num(d.queued_position) ?? 0,
      };
    }
  }
  return {
    running: ids(raw.running),
    runs: map(raw.runs, String),
    details: detailMap,
    awaiting: ids(raw.awaiting_approval),
    queued: map(raw.queued, (v) => num(v) ?? 0),
  };
}

/**
 * Asks the server to cancel a run. `runId` comes from `sendTurn`'s callback
 * or from `chatActivity().runs`; without it the server fails closed on
 * purpose (a stale tab must not cancel the run another tab just started),
 * so a Stop with no id is a Stop that does nothing.
 */
export async function stopChat(sessionId: string, runId?: string | null): Promise<boolean> {
  try {
    const response = await fetch(`/api/chat/stop/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: runId ? { [RUN_ID_HEADER]: runId } : undefined,
    });
    const body = (await response.json()) as { stopped?: unknown };
    return Boolean(body.stopped);
  } catch {
    /* the abort already closed our side; the server will notice */
    return false;
  }
}
