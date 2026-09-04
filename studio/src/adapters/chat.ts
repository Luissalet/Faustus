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
  folder: string | null;
  isImportant: boolean;
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

/** Everything the stream can say, narrowed to what the screen renders. */
export type ChatEvent =
  | { type: 'delta'; text: string; thinking: boolean }
  | { type: 'tool_start'; tool: string; command: string; fullCommand?: string; round: number }
  | { type: 'tool_progress'; tool: string; message: string }
  | { type: 'tool_output'; tool: string; command: string; output: string; exitCode: number | null }
  | { type: 'round'; round: number }
  | { type: 'ask_user'; ask: AskUser }
  | { type: 'metrics'; metrics: TurnMetrics }
  | { type: 'sources'; sources: WebSource[] }
  | { type: 'image'; url: string }
  | { type: 'fallback'; answeredBy: string; selected: string }
  | { type: 'terminal'; failed: boolean; message?: string }
  | { type: 'error'; message: string }
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
  folder?: string | null;
  is_important?: boolean;
}

export async function listSessions(signal?: AbortSignal): Promise<ChatSession[]> {
  const raw = asArray<RawSession>(await getJson<unknown>('/api/sessions', signal));
  return raw
    .map((s) => ({
      id: s.id,
      name: s.name?.trim() || 'Sin título',
      model: s.model ?? '',
      endpointUrl: s.endpoint_url ?? '',
      mode: (s.mode === 'agent' || s.mode === 'chat' ? s.mode : null) as ChatSession['mode'],
      messageCount: s.message_count ?? 0,
      lastMessageAt: s.last_message_at ?? null,
      folder: s.folder ?? null,
      isImportant: Boolean(s.is_important),
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
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .map((m) => ({
      role: m.role as 'user' | 'assistant',
      content: str(m.content),
      metadata: (m.metadata && typeof m.metadata === 'object' ? m.metadata : {}) as Record<
        string,
        unknown
      >,
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

export async function listModels(signal?: AbortSignal): Promise<ModelRoute[]> {
  const raw = await getJson<{ items?: unknown }>('/api/models?background=false', signal);
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
  workspace?: string;
  route?: ModelRoute | null;
  /** Answering a tool approval: the message goes empty and these travel. */
  approval?: { id: string; decision: 'approve' | 'approve_task' | 'deny' };
  signal?: AbortSignal;
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

/** One raw `data:` payload → zero or one typed events. */
function decode(raw: Record<string, unknown>, sseEvent: string | null): ChatEvent | null {
  if (sseEvent === 'error') {
    // The server says `text`; older paths say `error` or `message`.
    return {
      type: 'error',
      message: str(raw.text ?? raw.error ?? raw.message ?? raw.detail, 'Error del servidor'),
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
      };
    case 'agent_step':
      return { type: 'round', round: num(raw.round) ?? 1 };
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
      return {
        type: 'sources',
        sources: asArray<Record<string, unknown>>(raw.data)
          .map((s) => ({ title: str(s.title) || str(s.url), url: str(s.url) }))
          .filter((s) => s.url),
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
      return { type: 'error', message: str(raw.text ?? raw.error ?? raw.message, 'Error del servidor') };
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
  if (options.workspace) fd.append('workspace', options.workspace);
  if (options.route) {
    fd.append('selected_model', options.route.model);
    if (options.route.endpointUrl) fd.append('selected_endpoint_url', options.route.endpointUrl);
    if (options.route.endpointId) fd.append('selected_endpoint_id', options.route.endpointId);
  }
  if (options.approval) {
    fd.append('tool_approval_id', options.approval.id);
    fd.append('tool_approval_decision', options.approval.decision);
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
    yield { type: 'error', message: detail || `El servidor ha respondido ${response.status}` };
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
