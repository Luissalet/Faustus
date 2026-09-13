/**
 * The chat SSE wire: framing, the typed event union, and `decode()` — one
 * raw `data:` JSON payload (plus the optional `event:` name preceding it)
 * to zero or one typed `SseEvent`. CONTRATO_SDK_S2.md § S2.2/S2.3;
 * `docs/api/sse_events.json` is the source of truth this mirrors.
 */
import {
  CORE_EVENT_TYPES,
  type AgentStepEvent,
  type AgentTerminalEvent,
  type AskUserEvent,
  type AskUserResolvedEvent,
  type CapabilitiesChangedEvent,
  type ChatTerminalEvent,
  type ContextLedgerEvent,
  type ContextReceiptsEvent,
  type ErrorEvent,
  type FallbackEvent,
  type GitPolicyEvent,
  type HarnessSummaryEvent,
  type MessageSavedEvent,
  type MetricsEvent,
  type PlanUpdateEvent,
  type ProgressUpdateEvent,
  type RunActivityEvent,
  type StrategyEvent,
  type ToolApprovalResolvedEvent,
  type ToolEffectEvent,
  type ToolOutputEvent,
  type ToolProgressEvent,
  type ToolStartEvent,
  type UncertainEvent,
  type WebSourcesEvent,
} from './events.generated.js';
import { StreamAbortedError } from './errors.js';
import { num, str } from './util.js';

/** Text tokens: the one frame on the wire with no `type` field at all
 *  (`docs/api/sse_events.json`'s `{"type": null, "name": "delta", ...}`
 *  entry) — normalized here to `type: 'delta'` so every other event on the
 *  union is switchable the same way. */
export interface DeltaEvent {
  type: 'delta';
  delta: string;
  thinking?: boolean;
  sequence?: number;
  trace_id?: string;
  step_id?: string;
  stream_id?: string;
  schema_version?: string;
}

/** An `extended`-stability event (`docs/api/sse_events.json`), or any
 *  `type` this build's catalogue does not know about yet — forwarded
 *  as-is, never dropped, so a caller that wants it can still read it off
 *  the raw fields. `extended` events may change shape between minor
 *  versions; treat this as "here is what arrived", not a stable contract. */
export interface UnknownEvent {
  type: string;
  [key: string]: unknown;
}

export type SseEvent =
  | DeltaEvent
  | ToolStartEvent
  | ToolProgressEvent
  | ToolOutputEvent
  | ToolEffectEvent
  | AgentStepEvent
  | AskUserEvent
  | AskUserResolvedEvent
  | ToolApprovalResolvedEvent
  | RunActivityEvent
  | MetricsEvent
  | ChatTerminalEvent
  | AgentTerminalEvent
  | ErrorEvent
  | MessageSavedEvent
  | UncertainEvent
  | CapabilitiesChangedEvent
  | PlanUpdateEvent
  | ProgressUpdateEvent
  | HarnessSummaryEvent
  | ContextLedgerEvent
  | WebSourcesEvent
  | ContextReceiptsEvent
  | GitPolicyEvent
  | StrategyEvent
  | FallbackEvent
  | UnknownEvent;

const CORE_TYPES: ReadonlySet<string> = new Set(CORE_EVENT_TYPES);

function envelope(raw: Record<string, unknown>) {
  return {
    sequence: num(raw.sequence),
    trace_id: str(raw.trace_id),
    step_id: str(raw.step_id),
    stream_id: str(raw.stream_id),
    schema_version: str(raw.schema_version),
  };
}

/**
 * One raw `data:` JSON payload → zero or one typed `SseEvent`.
 *
 * `sseEvent` is the name from a preceding `event: <name>\n` line, or `null`
 * when the frame arrived as a bare `data:` line. `docs/api/sse_events.json`
 * `framing.named_events` lists `web_sources`/`context_receipts`/
 * `git_policy`/`error` as sometimes preceded by that line — every one of
 * those also carries its own `type` in the body except `error`, whose body
 * may carry no `type` at all when sent this way; that is the one case
 * `sseEvent` decides the type instead of `raw.type`.
 */
export function decode(raw: Record<string, unknown>, sseEvent: string | null): SseEvent | null {
  const env = envelope(raw);

  if (sseEvent === 'error') {
    return {
      type: 'error',
      text: str(raw.text),
      error: str(raw.error),
      message: str(raw.message),
      detail: str(raw.detail),
      error_class: str(raw.error_class),
      ...env,
    };
  }

  if (typeof raw.delta === 'string') {
    return { type: 'delta', delta: raw.delta, thinking: raw.thinking === true, ...env };
  }

  const type = str(raw.type);
  if (!type) return null;

  if (!CORE_TYPES.has(type)) {
    // extended, or unknown to this build's catalogue: forward everything.
    return { ...raw, type, ...env } as UnknownEvent;
  }

  const data = raw.data && typeof raw.data === 'object' ? (raw.data as Record<string, unknown>) : {};

  switch (type) {
    case 'tool_start':
      return {
        type: 'tool_start',
        tool: str(raw.tool) ?? '',
        command: str(raw.command),
        full_command: str(raw.full_command),
        round: num(raw.round),
        ...env,
      };
    case 'tool_progress':
      return {
        type: 'tool_progress',
        tool: str(raw.tool),
        message: str(raw.message),
        event: str(raw.event),
        subagent: raw.subagent && typeof raw.subagent === 'object' ? (raw.subagent as Record<string, unknown>) : undefined,
        ...env,
      };
    case 'tool_output':
      return {
        type: 'tool_output',
        tool: str(raw.tool) ?? '',
        command: str(raw.command),
        output: str(raw.output),
        exit_code: num(raw.exit_code),
        diff: str(raw.diff),
        doc_id: str(raw.doc_id),
        screenshot: str(raw.screenshot),
        argument_errors: Array.isArray(raw.argument_errors) ? raw.argument_errors : undefined,
        repairs: Array.isArray(raw.repairs) ? raw.repairs : undefined,
        evidence_refs: Array.isArray(raw.evidence_refs) ? raw.evidence_refs : undefined,
        execution_target:
          raw.execution_target && typeof raw.execution_target === 'object'
            ? (raw.execution_target as Record<string, unknown>)
            : undefined,
        call_id: str(raw.call_id),
        ...env,
      };
    case 'tool_effect':
      return {
        type: 'tool_effect',
        call_id: str(raw.call_id) ?? '',
        tool: str(raw.tool) ?? '',
        effect_class: str(raw.effect_class) ?? '',
        state: (str(raw.state) as ToolEffectEvent['state']) ?? 'pending',
        idempotency_key: str(raw.idempotency_key) ?? '',
        ...env,
      };
    case 'agent_step':
      return { type: 'agent_step', round: num(raw.round) ?? 0, ...env };
    case 'ask_user':
      return {
        type: 'ask_user',
        data: {
          question: str(data.question) ?? '',
          options: Array.isArray(data.options) ? data.options : [],
          multi: data.multi === true,
          kind: data.kind === 'tool_approval' ? 'tool_approval' : 'question',
          approval_id: str(data.approval_id),
          question_id: str(data.question_id),
          revision: num(data.revision),
          allow_free_text: data.allow_free_text === true,
        },
        ...env,
      };
    case 'ask_user_resolved':
      return { type: 'ask_user_resolved', ...env };
    case 'tool_approval_resolved':
      return { type: 'tool_approval_resolved', ...env };
    case 'run_activity':
      return {
        type: 'run_activity',
        data: {
          phase: str(data.phase) ?? '',
          phase_since: num(data.phase_since),
          tool: str(data.tool),
          detail: str(data.detail),
          model_state: str(data.model_state),
          round: num(data.round),
        },
        heartbeat: num(raw.heartbeat),
        ...env,
      };
    case 'metrics':
      return { type: 'metrics', data, ...env };
    case 'chat_terminal':
      return { type: 'chat_terminal', data, ...env };
    case 'agent_terminal': {
      const failure = data.failure && typeof data.failure === 'object' ? (data.failure as Record<string, unknown>) : undefined;
      return {
        type: 'agent_terminal',
        data: {
          failure: failure ? { message: str(failure.message) ?? '', error_class: str(failure.error_class) ?? '' } : undefined,
        },
        ...env,
      };
    }
    case 'error':
      return {
        type: 'error',
        text: str(raw.text),
        error: str(raw.error),
        message: str(raw.message),
        detail: str(raw.detail),
        error_class: str(raw.error_class),
        ...env,
      };
    case 'message_saved':
      return { type: 'message_saved', id: str(raw.id) ?? '', ...env };
    case 'uncertain':
      return { type: 'uncertain', status: str(raw.status) ?? '', detail: str(raw.detail), ...env };
    case 'capabilities_changed':
      return {
        type: 'capabilities_changed',
        data: {
          from_model: str(data.from_model) ?? '',
          to_model: str(data.to_model) ?? '',
          lost: Array.isArray(data.lost) ? data.lost : [],
        },
        ...env,
      };
    case 'plan_update':
      return {
        type: 'plan_update',
        data: {
          plan: data.plan,
          steps: Array.isArray(data.steps) ? data.steps : [],
          revision: num(data.revision) ?? 0,
          warnings: Array.isArray(data.warnings) ? data.warnings : [],
        },
        ...env,
      };
    case 'progress_update':
      return { type: 'progress_update', todos: Array.isArray(raw.todos) ? raw.todos : [], ...env };
    case 'harness_summary':
      return { type: 'harness_summary', data, ...env };
    case 'context_ledger':
      return { type: 'context_ledger', data, ...env };
    case 'web_sources':
      return { type: 'web_sources', data: Array.isArray(raw.data) ? raw.data : [], ...env };
    case 'context_receipts':
      return { type: 'context_receipts', data: Array.isArray(raw.data) ? raw.data : [], ...env };
    case 'git_policy':
      return {
        type: 'git_policy',
        action: (str(raw.action) as GitPolicyEvent['action']) ?? 'branch',
        ok: raw.ok === true,
        branch: str(raw.branch),
        sha: str(raw.sha),
        detail: str(raw.detail),
        ...env,
      };
    case 'strategy':
      return { type: 'strategy', data, ...env };
    case 'fallback':
      return { type: 'fallback', answered_by: str(raw.answered_by), selected_model: str(raw.selected_model), ...env };
    default:
      // Unreachable: every CORE_TYPES member is handled above. A new core
      // type added to the catalogue without a matching `case` here falls
      // back to UnknownEvent rather than silently disappearing.
      return { ...raw, type, ...env } as UnknownEvent;
  }
}

/**
 * Reads one SSE body, calling `onFrame` for every decoded `data:` payload,
 * until the literal `data: [DONE]` terminator (resolves `'done'`) or the
 * body/connection breaks (throws). Never retries anything itself — that is
 * `Turn`'s job (`turn.ts`); this only knows how to read one stream once.
 */
export async function pumpBody(
  body: ReadableStream<Uint8Array>,
  onFrame: (raw: Record<string, unknown>, sseEvent: string | null) => void,
  opts: { signal?: AbortSignal; idleTimeoutMs: number },
): Promise<'done'> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sseEvent: string | null = null;

  try {
    for (;;) {
      if (opts.signal?.aborted) throw new StreamAbortedError();

      let timer: ReturnType<typeof setTimeout> | undefined;
      const idle = new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`No data received for ${opts.idleTimeoutMs}ms`)), opts.idleTimeoutMs);
      });
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        result = await Promise.race([reader.read(), idle]);
      } finally {
        if (timer) clearTimeout(timer);
      }

      if (result.done) throw new Error('The stream ended before [DONE].');
      buffer += decoder.decode(result.value, { stream: true });

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
        if (payload === '[DONE]') return 'done';
        let raw: unknown;
        try {
          raw = JSON.parse(payload);
        } catch {
          sseEvent = null;
          continue;
        }
        if (raw && typeof raw === 'object') onFrame(raw as Record<string, unknown>, sseEvent);
        sseEvent = null;
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* already released by a failed read */
    }
  }
}
